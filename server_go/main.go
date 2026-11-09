package main

import (
	"crypto/md5"
	"crypto/subtle"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"
)

//go:embed static/index.html
var embeddedIndexHTML []byte

// ==================== 1. 配置结构 ====================

type Config struct {
	ListenAddr            string  `json:"listen_addr"`
	ClientPassword        string  `json:"client_password"`
	NodeAPIKey            string  `json:"node_api_key"`
	PCHeartbeatTimeoutSec float64 `json:"pc_heartbeat_timeout_sec"`
	DedupTTLSec           float64 `json:"dedup_ttl_sec"`
	MaxMessagesPerConv    int     `json:"max_messages_per_conv"`
	DataFile              string  `json:"data_file"`
}

func loadConfig(cfgPath string) Config {
	cfg := Config{
		ListenAddr:            "0.0.0.0:8000",
		ClientPassword:        "xz1c-secret-2026",
		NodeAPIKey:            "node-static-key-9527",
		PCHeartbeatTimeoutSec: 15.0,
		DedupTTLSec:           5.0,
		MaxMessagesPerConv:    200,
		DataFile:              "relay_store.json",
	}

	data, err := os.ReadFile(cfgPath)
	if err != nil {
		log.Printf("[Config] config.json not found at %s (%v), using default 0.0.0.0:8000", cfgPath, err)
	} else {
		if err := json.Unmarshal(data, &cfg); err != nil {
			log.Printf("[Config ERROR] Failed to parse %s: %v (Please check JSON syntax)", cfgPath, err)
		} else {
			log.Printf("[Config] Successfully loaded configuration from %s", cfgPath)
		}
	}

	// 容错处理：如果用户只写了 "0.0.0.0" 没带端口，自动补齐 ":8000"
	if !strings.Contains(cfg.ListenAddr, ":") {
		cfg.ListenAddr = cfg.ListenAddr + ":8000"
	}

	if v := os.Getenv("LISTEN_ADDR"); v != "" {
		cfg.ListenAddr = v
	}
	if v := os.Getenv("CLIENT_PASSWORD"); v != "" {
		cfg.ClientPassword = v
	}
	if v := os.Getenv("NODE_API_KEY"); v != "" {
		cfg.NodeAPIKey = v
	}
	if v := os.Getenv("PC_HEARTBEAT_TIMEOUT"); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil && f > 0 {
			cfg.PCHeartbeatTimeoutSec = f
		}
	}
	if v := os.Getenv("DEDUP_TTL_SECONDS"); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil && f > 0 {
			cfg.DedupTTLSec = f
		}
	}
	return cfg
}

func constantTimeEquals(candidate, secret string) bool {
	c := strings.TrimSpace(strings.TrimPrefix(candidate, "Bearer "))
	if c == "" || secret == "" {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(c), []byte(secret)) == 1
}

// ==================== 2. 5 秒滑动窗口内存去重器 ====================

type dedupEntry struct {
	key       string
	createdAt time.Time
}

type MessageDeduplicator struct {
	mu      sync.Mutex
	ttl     time.Duration
	maxSize int
	entries []dedupEntry
	index   map[string]time.Time
}

func NewMessageDeduplicator(ttlSeconds float64, maxSize int) *MessageDeduplicator {
	return &MessageDeduplicator{
		ttl:     time.Duration(ttlSeconds * float64(time.Second)),
		maxSize: maxSize,
		entries: make([]dedupEntry, 0, 128),
		index:   make(map[string]time.Time, 128),
	}
}

func computeFingerprint(sender, content string, isGroup bool, roomName string) string {
	gFlag := "0"
	normRoom := ""
	if isGroup {
		gFlag = "1"
		normRoom = strings.TrimSpace(roomName)
	}
	raw := fmt.Sprintf("%s|%s|%s|%s", gFlag, normRoom, strings.TrimSpace(sender), strings.TrimSpace(content))
	sum := md5.Sum([]byte(raw))
	return hex.EncodeToString(sum[:])
}

func (d *MessageDeduplicator) IsDuplicate(sender, content string, isGroup bool, roomName string) (bool, string) {
	now := time.Now()
	fp := computeFingerprint(sender, content, isGroup, roomName)

	d.mu.Lock()
	defer d.mu.Unlock()

	// 1. 清理过期头部记录
	validIdx := 0
	for validIdx < len(d.entries) {
		if now.Sub(d.entries[validIdx].createdAt) > d.ttl {
			delete(d.index, d.entries[validIdx].key)
			validIdx++
		} else {
			break
		}
	}
	if validIdx > 0 {
		d.entries = d.entries[validIdx:]
	}

	// 2. 检查是否命中未过期指纹
	if ts, exists := d.index[fp]; exists && now.Sub(ts) <= d.ttl {
		return true, fp
	}

	// 3. 插入新指纹并限制最大容量
	d.index[fp] = now
	d.entries = append(d.entries, dedupEntry{key: fp, createdAt: now})
	for len(d.entries) > d.maxSize {
		oldest := d.entries[0]
		d.entries = d.entries[1:]
		delete(d.index, oldest.key)
	}

	return false, fp
}

func (d *MessageDeduplicator) Size() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return len(d.index)
}

// ==================== 3. 极轻量内存 + 原子快照存储 (零 CGO) ====================

type Conversation struct {
	ConvID        string `json:"conv_id"`
	Title         string `json:"title"`
	IsGroup       bool   `json:"is_group"`
	LastSender    string `json:"last_sender"`
	LastContent   string `json:"last_content"`
	LastTimestamp int64  `json:"last_timestamp"`
	UnreadCount   int    `json:"unread_count"`
}

type ChatMessage struct {
	ID         int64  `json:"id"`
	ConvID     string `json:"conv_id"`
	Title      string `json:"title"`
	Sender     string `json:"sender"`
	Content    string `json:"content"`
	Timestamp  int64  `json:"timestamp"`
	SourceNode string `json:"source_node"`
	IsGroup    bool   `json:"is_group"`
	RoomName   string `json:"room_name"`
	Direction  string `json:"direction"` // "in" or "out"
	Status     string `json:"status"`
}

type diskSnapshot struct {
	NextMsgID     int64                     `json:"next_msg_id"`
	Conversations map[string]*Conversation  `json:"conversations"`
	Messages      map[string][]*ChatMessage `json:"messages"`
}

type MessageStore struct {
	mu            sync.RWMutex
	filePath      string
	maxPerConv    int
	nextMsgID     int64
	conversations map[string]*Conversation
	messages      map[string][]*ChatMessage
}

func NewMessageStore(filePath string, maxPerConv int) *MessageStore {
	s := &MessageStore{
		filePath:      filePath,
		maxPerConv:    maxPerConv,
		nextMsgID:     1,
		conversations: make(map[string]*Conversation),
		messages:      make(map[string][]*ChatMessage),
	}
	s.loadFromDisk()
	return s
}

func makeConvID(sender string, isGroup bool, roomName string) (string, string) {
	r := strings.TrimSpace(roomName)
	s := strings.TrimSpace(sender)
	if isGroup && r != "" {
		return "group:" + r, r
	}
	return "user:" + s, s
}

func (s *MessageStore) SaveMessage(sender, content string, ts int64, sourceNode string, isGroup bool, roomName, direction, status string) *ChatMessage {
	if ts <= 0 {
		ts = time.Now().Unix()
	}
	convID, title := makeConvID(sender, isGroup, roomName)

	s.mu.Lock()
	msgID := s.nextMsgID
	s.nextMsgID++

	msg := &ChatMessage{
		ID:         msgID,
		ConvID:     convID,
		Title:      title,
		Sender:     strings.TrimSpace(sender),
		Content:    content,
		Timestamp:  ts,
		SourceNode: sourceNode,
		IsGroup:    isGroup,
		RoomName:   strings.TrimSpace(roomName),
		Direction:  direction,
		Status:     status,
	}

	list := append(s.messages[convID], msg)
	if len(list) > s.maxPerConv {
		list = list[len(list)-s.maxPerConv:]
	}
	s.messages[convID] = list

	conv, exists := s.conversations[convID]
	if !exists {
		conv = &Conversation{
			ConvID:  convID,
			Title:   title,
			IsGroup: isGroup,
		}
		s.conversations[convID] = conv
	}
	conv.Title = title
	conv.IsGroup = isGroup
	conv.LastSender = msg.Sender
	conv.LastContent = msg.Content
	if ts >= conv.LastTimestamp {
		conv.LastTimestamp = ts
	}
	if direction == "in" {
		conv.UnreadCount++
	} else {
		conv.UnreadCount = 0
	}
	s.mu.Unlock()

	go s.persistToDisk()
	return msg
}

func (s *MessageStore) ListConversations() []*Conversation {
	s.mu.RLock()
	defer s.mu.RUnlock()

	res := make([]*Conversation, 0, len(s.conversations))
	for _, c := range s.conversations {
		cp := *c
		res = append(res, &cp)
	}
	sort.Slice(res, func(i, j int) bool {
		return res[i].LastTimestamp > res[j].LastTimestamp
	})
	return res
}

func (s *MessageStore) GetMessages(convID string, limit int) []*ChatMessage {
	s.mu.Lock()
	if c, ok := s.conversations[convID]; ok {
		c.UnreadCount = 0
	}
	list := s.messages[convID]
	start := 0
	if limit > 0 && len(list) > limit {
		start = len(list) - limit
	}
	res := make([]*ChatMessage, 0, len(list)-start)
	for _, m := range list[start:] {
		cp := *m
		res = append(res, &cp)
	}
	s.mu.Unlock()

	go s.persistToDisk()
	return res
}

func (s *MessageStore) ClearUnread(convID string) {
	s.mu.Lock()
	if c, ok := s.conversations[convID]; ok {
		c.UnreadCount = 0
	}
	s.mu.Unlock()
}

func (s *MessageStore) loadFromDisk() {
	data, err := os.ReadFile(s.filePath)
	if err != nil {
		return
	}
	var snap diskSnapshot
	if err := json.Unmarshal(data, &snap); err == nil {
		if snap.NextMsgID > 0 {
			s.nextMsgID = snap.NextMsgID
		}
		if snap.Conversations != nil {
			s.conversations = snap.Conversations
		}
		if snap.Messages != nil {
			s.messages = snap.Messages
		}
	}
}

func (s *MessageStore) persistToDisk() {
	s.mu.RLock()
	snap := diskSnapshot{
		NextMsgID:     s.nextMsgID,
		Conversations: s.conversations,
		Messages:      s.messages,
	}
	raw, err := json.Marshal(snap)
	s.mu.RUnlock()
	if err != nil {
		return
	}
	tmpPath := s.filePath + ".tmp"
	if err := os.WriteFile(tmpPath, raw, 0600); err == nil {
		_ = os.Rename(tmpPath, s.filePath)
	}
}

// ==================== 4. 主备路由状态机与 WebSocket 集线器 ====================

type ReplyCommand struct {
	CommandID  string `json:"command_id"`
	Action     string `json:"action"`
	Target     string `json:"target"`
	Content    string `json:"content"`
	IsGroup    bool   `json:"is_group"`
	RoomName   string `json:"room_name"`
	ConvID     string `json:"conv_id"`
	RoutedNode string `json:"routed_node"`
	Timestamp  int64  `json:"timestamp"`
}

type wsClientConn struct {
	conn *websocket.Conn
	mu   sync.Mutex
}

func (c *wsClientConn) writeJSON(v any) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	_ = c.conn.SetWriteDeadline(time.Now().Add(8 * time.Second))
	return c.conn.WriteJSON(v)
}

type RelayHub struct {
	cfg                Config
	store              *MessageStore
	dedup              *MessageDeduplicator
	mu                 sync.Mutex
	lastPCHeartbeat    time.Time
	lastMobHeartbeat   time.Time
	lastBroadcastPCOn  int32 // -1: unknown, 0: false, 1: true
	cmdSeq             uint64
	clientSockets      map[*wsClientConn]struct{}
	nodeSockets        map[string]map[*wsClientConn]struct{}
	pendingCommands    map[string][]ReplyCommand
	mobilePollWakeupCh chan struct{}
}

func NewRelayHub(cfg Config, store *MessageStore, dedup *MessageDeduplicator) *RelayHub {
	return &RelayHub{
		cfg:               cfg,
		store:             store,
		dedup:             dedup,
		lastBroadcastPCOn: -1,
		clientSockets:     make(map[*wsClientConn]struct{}),
		nodeSockets: map[string]map[*wsClientConn]struct{}{
			"pc":     make(map[*wsClientConn]struct{}),
			"mobile": make(map[*wsClientConn]struct{}),
		},
		pendingCommands: map[string][]ReplyCommand{
			"pc":     make([]ReplyCommand, 0),
			"mobile": make([]ReplyCommand, 0),
		},
		mobilePollWakeupCh: make(chan struct{}, 16),
	}
}

func (h *RelayHub) RecordHeartbeat(nodeType string) map[string]any {
	now := time.Now()
	norm := strings.ToLower(strings.TrimSpace(nodeType))
	h.mu.Lock()
	if norm == "pc" {
		h.lastPCHeartbeat = now
	} else {
		h.lastMobHeartbeat = now
	}
	h.mu.Unlock()
	return h.GetStatusSnapshot()
}

func (h *RelayHub) IsPCOnline() bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.lastPCHeartbeat.IsZero() {
		return false
	}
	timeout := time.Duration(h.cfg.PCHeartbeatTimeoutSec * float64(time.Second))
	return time.Since(h.lastPCHeartbeat) < timeout
}

func (h *RelayHub) GetStatusSnapshot() map[string]any {
	h.mu.Lock()
	defer h.mu.Unlock()

	timeout := time.Duration(h.cfg.PCHeartbeatTimeoutSec * float64(time.Second))
	pcOnline := !h.lastPCHeartbeat.IsZero() && time.Since(h.lastPCHeartbeat) < timeout
	pcState := "PC_OFFLINE"
	activeNode := "mobile"
	activeLabel := "手机备用节点"
	if pcOnline {
		pcState = "PC_ONLINE"
		activeNode = "pc"
		activeLabel = "PC 主节点"
	}

	var pcAge any = nil
	if !h.lastPCHeartbeat.IsZero() {
		pcAge = float64(int(time.Since(h.lastPCHeartbeat).Seconds()*10)) / 10.0
	}
	var mobAge any = nil
	if !h.lastMobHeartbeat.IsZero() {
		mobAge = float64(int(time.Since(h.lastMobHeartbeat).Seconds()*10)) / 10.0
	}

	return map[string]any{
		"pc_state":                   pcState,
		"pc_online":                  pcOnline,
		"active_node":                activeNode,
		"active_node_label":          activeLabel,
		"pc_ws_connected":            len(h.nodeSockets["pc"]) > 0,
		"mobile_ws_connected":        len(h.nodeSockets["mobile"]) > 0,
		"pc_last_seen_seconds_ago":   pcAge,
		"mobile_last_seen_seconds_ago": mobAge,
		"pending_mobile_commands":    len(h.pendingCommands["mobile"]),
		"timestamp":                  time.Now().Unix(),
	}
}

func (h *RelayHub) BroadcastToClients(payload map[string]any) {
	h.mu.Lock()
	clients := make([]*wsClientConn, 0, len(h.clientSockets))
	for c := range h.clientSockets {
		clients = append(clients, c)
	}
	h.mu.Unlock()

	for _, c := range clients {
		if err := c.writeJSON(payload); err != nil {
			h.mu.Lock()
			delete(h.clientSockets, c)
			h.mu.Unlock()
			_ = c.conn.Close()
		}
	}
}

func (h *RelayHub) BroadcastStatusIfChanged(force bool) {
	snap := h.GetStatusSnapshot()
	pcOnline, _ := snap["pc_online"].(bool)
	var curVal int32 = 0
	if pcOnline {
		curVal = 1
	}
	prev := atomic.SwapInt32(&h.lastBroadcastPCOn, curVal)
	if force || prev != curVal {
		h.BroadcastToClients(map[string]any{
			"type": "node_status",
			"data": snap,
		})
	}
}

func (h *RelayHub) WatchdogLoop() {
	ticker := time.NewTicker(1 * time.Second)
	defer ticker.Stop()
	for range ticker.C {
		h.BroadcastStatusIfChanged(false)
	}
}

func (h *RelayHub) DispatchReplyCommand(target, content string, isGroup bool, roomName, convID, preferredNode string) map[string]any {
	routeNode := "mobile"
	if preferredNode == "pc" || preferredNode == "mobile" {
		routeNode = preferredNode
	} else if h.IsPCOnline() {
		routeNode = "pc"
	}

	seq := atomic.AddUint64(&h.cmdSeq, 1)
	cmd := ReplyCommand{
		CommandID:  fmt.Sprintf("cmd_%d_%d", time.Now().UnixMilli(), seq),
		Action:     "send_wechat_msg",
		Target:     target,
		Content:    content,
		IsGroup:    isGroup,
		RoomName:   roomName,
		ConvID:     convID,
		RoutedNode: routeNode,
		Timestamp:  time.Now().Unix(),
	}

	if h.sendToNodeWS(routeNode, cmd) {
		return map[string]any{
			"routed_node":   routeNode,
			"delivery_mode": "websocket",
			"status":        "sent_to_node",
			"command":       cmd,
		}
	}

	// 若原定发往 PC，但 PC WS 投递失败，立即降级至手机节点
	if routeNode == "pc" {
		log.Printf("[Failover] PC WS delivery failed, failing over to mobile node immediately.")
		h.mu.Lock()
		h.lastPCHeartbeat = time.Time{}
		h.mu.Unlock()

		routeNode = "mobile"
		cmd.RoutedNode = "mobile"
		h.BroadcastStatusIfChanged(true)

		if h.sendToNodeWS("mobile", cmd) {
			return map[string]any{
				"routed_node":   "mobile",
				"delivery_mode": "websocket_failover",
				"status":        "sent_to_node",
				"command":       cmd,
			}
		}
	}

	// 推入内存拉取队列并唤醒长轮询
	h.mu.Lock()
	q := append(h.pendingCommands[routeNode], cmd)
	if len(q) > 100 {
		q = q[len(q)-100:]
	}
	h.pendingCommands[routeNode] = q
	h.mu.Unlock()

	select {
	case h.mobilePollWakeupCh <- struct{}{}:
	default:
	}

	return map[string]any{
		"routed_node":   routeNode,
		"delivery_mode": "queued_for_poll",
		"status":        "queued",
		"command":       cmd,
	}
}

func (h *RelayHub) sendToNodeWS(nodeType string, cmd ReplyCommand) bool {
	h.mu.Lock()
	set := h.nodeSockets[nodeType]
	conns := make([]*wsClientConn, 0, len(set))
	for c := range set {
		conns = append(conns, c)
	}
	h.mu.Unlock()

	if len(conns) == 0 {
		return false
	}

	sent := false
	for _, c := range conns {
		err := c.writeJSON(map[string]any{
			"type": "reply_command",
			"data": cmd,
		})
		if err == nil {
			sent = true
		} else {
			h.mu.Lock()
			delete(h.nodeSockets[nodeType], c)
			h.mu.Unlock()
			_ = c.conn.Close()
		}
	}
	return sent
}

func (h *RelayHub) PollPendingCommands(nodeType string, waitSec float64) []ReplyCommand {
	norm := "mobile"
	if strings.ToLower(strings.TrimSpace(nodeType)) == "pc" {
		norm = "pc"
	}
	h.RecordHeartbeat(norm)

	h.mu.Lock()
	hasItems := len(h.pendingCommands[norm]) > 0
	h.mu.Unlock()

	if !hasItems && waitSec > 0 {
		if waitSec > 25 {
			waitSec = 25
		}
		timer := time.NewTimer(time.Duration(waitSec * float64(time.Second)))
		select {
		case <-h.mobilePollWakeupCh:
		case <-timer.C:
		}
		timer.Stop()
	}

	h.mu.Lock()
	items := h.pendingCommands[norm]
	h.pendingCommands[norm] = make([]ReplyCommand, 0)
	h.mu.Unlock()
	if items == nil {
		return []ReplyCommand{}
	}
	return items
}

// ==================== 5. HTTP & WebSocket 路由处理器 ====================

var wsUpgrader = websocket.Upgrader{
	CheckOrigin: func(r *http.Request) bool { return true },
}

func writeJSON(w http.ResponseWriter, status int, data any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(data)
}

func checkClientAuth(r *http.Request, secret string) bool {
	tok := r.Header.Get("X-Client-Token")
	if tok == "" {
		tok = r.Header.Get("Authorization")
	}
	if tok == "" {
		tok = r.URL.Query().Get("token")
	}
	return constantTimeEquals(tok, secret)
}

func checkNodeAuth(r *http.Request, secret string) bool {
	key := r.Header.Get("X-API-Key")
	if key == "" {
		key = r.Header.Get("Authorization")
	}
	if key == "" {
		key = r.URL.Query().Get("api_key")
	}
	return constantTimeEquals(key, secret)
}

func main() {
	exePath, _ := os.Executable()
	exeDir := filepath.Dir(exePath)

	cfgPathFlag := flag.String("config", filepath.Join(exeDir, "config.json"), "Path to config.json")
	flag.Parse()

	// 若当前工作目录存在 config.json 也可优先读取
	cfgPath := *cfgPathFlag
	if _, err := os.Stat("config.json"); err == nil {
		cfgPath = "config.json"
	}

	cfg := loadConfig(cfgPath)
	store := NewMessageStore(cfg.DataFile, cfg.MaxMessagesPerConv)
	dedup := NewMessageDeduplicator(cfg.DedupTTLSec, 512)
	hub := NewRelayHub(cfg, store, dedup)

	go hub.WatchdogLoop()

	mux := http.NewServeMux()

	// 1. 根路径返回内嵌极简页面 (备用)
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write(embeddedIndexHTML)
	})

	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "ts": time.Now().Unix()})
	})

	// 2. 客户端登录密码校验
	mux.HandleFunc("/api/auth/verify", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Password string `json:"password"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		if !constantTimeEquals(body.Password, cfg.ClientPassword) {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"ok": false, "detail": "密码错误"})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"ok":          true,
			"node_status": hub.GetStatusSnapshot(),
		})
	})

	// 3. 查询中枢状态
	mux.HandleFunc("/api/status", func(w http.ResponseWriter, r *http.Request) {
		if !checkClientAuth(r, cfg.ClientPassword) {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"detail": "Unauthorized"})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"ok":               true,
			"node_status":      hub.GetStatusSnapshot(),
			"dedup_cache_size": dedup.Size(),
		})
	})

	// 4. 节点心跳接口 (/api/heartbeat)
	mux.HandleFunc("/api/heartbeat", func(w http.ResponseWriter, r *http.Request) {
		if !checkNodeAuth(r, cfg.NodeAPIKey) {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"detail": "Invalid API_KEY"})
			return
		}
		var body struct {
			NodeType string `json:"node_type"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		if body.NodeType == "" {
			body.NodeType = "pc"
		}
		snap := hub.RecordHeartbeat(body.NodeType)
		hub.BroadcastStatusIfChanged(false)
		cmds := hub.PollPendingCommands(body.NodeType, 0)
		writeJSON(w, http.StatusOK, map[string]any{
			"ok":          true,
			"node_status": snap,
			"commands":    cmds,
		})
	})

	// 5. 上行微信新消息接口 (/api/webhook/message)
	handleUplinkMessage := func(sender, content string, ts int64, sourceNode string, isGroup bool, roomName string) map[string]any {
		normSource := "mobile"
		if strings.ToLower(strings.TrimSpace(sourceNode)) == "pc" {
			normSource = "pc"
		}
		hub.RecordHeartbeat(normSource)
		hub.BroadcastStatusIfChanged(false)

		isDup, fp := dedup.IsDuplicate(sender, content, isGroup, roomName)
		if isDup {
			log.Printf("[Dedup] Ignored duplicate message [%s] from %s (sender=%s)", fp[:8], normSource, sender)
			return map[string]any{
				"ok":           true,
				"deduplicated": true,
				"fingerprint":  fp,
				"source_node":  normSource,
			}
		}

		saved := store.SaveMessage(sender, content, ts, normSource, isGroup, roomName, "in", "delivered")
		hub.BroadcastToClients(map[string]any{
			"type":        "new_message",
			"data":        saved,
			"node_status": hub.GetStatusSnapshot(),
		})
		log.Printf("[Relay] New message [%s] via %s -> conv=%s sender=%s", fp[:8], normSource, saved.ConvID, saved.Sender)
		return map[string]any{
			"ok":           true,
			"deduplicated": false,
			"fingerprint":  fp,
			"message":      saved,
		}
	}

	mux.HandleFunc("/api/webhook/message", func(w http.ResponseWriter, r *http.Request) {
		if !checkNodeAuth(r, cfg.NodeAPIKey) {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"detail": "Invalid API_KEY"})
			return
		}
		var body struct {
			Sender     string `json:"sender"`
			Content    string `json:"content"`
			Timestamp  int64  `json:"timestamp"`
			SourceNode string `json:"source_node"`
			IsGroup    bool   `json:"is_group"`
			RoomName   string `json:"room_name"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil || strings.TrimSpace(body.Sender) == "" || strings.TrimSpace(body.Content) == "" {
			writeJSON(w, http.StatusBadRequest, map[string]any{"detail": "sender and content required"})
			return
		}
		res := handleUplinkMessage(body.Sender, body.Content, body.Timestamp, body.SourceNode, body.IsGroup, body.RoomName)
		writeJSON(w, http.StatusOK, res)
	})

	// 6. 节点长轮询拉取回复指令 (/api/node/commands)
	mux.HandleFunc("/api/node/commands", func(w http.ResponseWriter, r *http.Request) {
		if !checkNodeAuth(r, cfg.NodeAPIKey) {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"detail": "Invalid API_KEY"})
			return
		}
		nodeType := r.URL.Query().Get("node_type")
		if nodeType == "" {
			nodeType = "mobile"
		}
		waitSec, _ := strconv.ParseFloat(r.URL.Query().Get("wait"), 64)
		cmds := hub.PollPendingCommands(nodeType, waitSec)
		hub.BroadcastStatusIfChanged(false)
		writeJSON(w, http.StatusOK, map[string]any{
			"ok":        true,
			"node_type": nodeType,
			"count":     len(cmds),
			"commands":  cmds,
		})
	})

	// 7. 会话列表与历史消息查询
	mux.HandleFunc("/api/conversations", func(w http.ResponseWriter, r *http.Request) {
		if !checkClientAuth(r, cfg.ClientPassword) {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"detail": "Unauthorized"})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"ok":            true,
			"conversations": store.ListConversations(),
			"node_status":   hub.GetStatusSnapshot(),
		})
	})

	mux.HandleFunc("/api/messages", func(w http.ResponseWriter, r *http.Request) {
		if !checkClientAuth(r, cfg.ClientPassword) {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"detail": "Unauthorized"})
			return
		}
		convID := r.URL.Query().Get("conv_id")
		limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
		if limit <= 0 {
			limit = 80
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"ok":       true,
			"conv_id":  convID,
			"messages": store.GetMessages(convID, limit),
		})
	})

	// 8. 下行发送回复接口 (/api/reply)
	handleClientReply := func(target, content string, isGroup bool, roomName, convID, preferredNode string) map[string]any {
		targetName := strings.TrimSpace(target)
		if isGroup && strings.TrimSpace(roomName) != "" {
			targetName = strings.TrimSpace(roomName)
		}
		dispatch := hub.DispatchReplyCommand(targetName, content, isGroup, roomName, convID, preferredNode)
		routedNode, _ := dispatch["routed_node"].(string)
		statusStr, _ := dispatch["status"].(string)
		senderLabel := targetName
		if isGroup {
			senderLabel = "我"
		}
		savedOut := store.SaveMessage(senderLabel, content, time.Now().Unix(), routedNode, isGroup, targetName, "out", statusStr)
		hub.BroadcastToClients(map[string]any{
			"type":        "outbound_message",
			"data":        savedOut,
			"dispatch":    dispatch,
			"node_status": hub.GetStatusSnapshot(),
		})
		return map[string]any{
			"ok":          true,
			"message":     savedOut,
			"dispatch":    dispatch,
			"node_status": hub.GetStatusSnapshot(),
		}
	}

	mux.HandleFunc("/api/reply", func(w http.ResponseWriter, r *http.Request) {
		if !checkClientAuth(r, cfg.ClientPassword) {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"detail": "Unauthorized"})
			return
		}
		var body struct {
			Target        string `json:"target"`
			Content       string `json:"content"`
			IsGroup       bool   `json:"is_group"`
			RoomName      string `json:"room_name"`
			ConvID        string `json:"conv_id"`
			PreferredNode string `json:"preferred_node"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil || strings.TrimSpace(body.Target) == "" || strings.TrimSpace(body.Content) == "" {
			writeJSON(w, http.StatusBadRequest, map[string]any{"detail": "target and content required"})
			return
		}
		res := handleClientReply(body.Target, body.Content, body.IsGroup, body.RoomName, body.ConvID, body.PreferredNode)
		writeJSON(w, http.StatusOK, res)
	})

	// 9. 客户端 WebSocket (/ws/client) —— 供 Sony XZ1 原生 App 连接
	mux.HandleFunc("/ws/client", func(w http.ResponseWriter, r *http.Request) {
		if !checkClientAuth(r, cfg.ClientPassword) {
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}
		rawConn, err := wsUpgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		client := &wsClientConn{conn: rawConn}
		hub.mu.Lock()
		hub.clientSockets[client] = struct{}{}
		hub.mu.Unlock()

		_ = client.writeJSON(map[string]any{
			"type": "node_status",
			"data": hub.GetStatusSnapshot(),
		})

		defer func() {
			hub.mu.Lock()
			delete(hub.clientSockets, client)
			hub.mu.Unlock()
			_ = rawConn.Close()
		}()

		for {
			var pkt map[string]any
			if err := rawConn.ReadJSON(&pkt); err != nil {
				break
			}
			action, _ := pkt["action"].(string)
			if action == "" {
				action, _ = pkt["type"].(string)
			}
			switch action {
			case "ping":
				_ = client.writeJSON(map[string]any{
					"type": "pong",
					"data": hub.GetStatusSnapshot(),
				})
			case "mark_read":
				if cid, ok := pkt["conv_id"].(string); ok && cid != "" {
					store.ClearUnread(cid)
				}
			case "send_reply":
				target, _ := pkt["target"].(string)
				content, _ := pkt["content"].(string)
				isGroup, _ := pkt["is_group"].(bool)
				roomName, _ := pkt["room_name"].(string)
				convID, _ := pkt["conv_id"].(string)
				pref, _ := pkt["preferred_node"].(string)
				if strings.TrimSpace(target) != "" && strings.TrimSpace(content) != "" {
					handleClientReply(target, content, isGroup, roomName, convID, pref)
				}
			}
		}
	})

	// 10. 节点 WebSocket (/ws/node) —— 供 PC 节点主动反向长连接
	mux.HandleFunc("/ws/node", func(w http.ResponseWriter, r *http.Request) {
		if !checkNodeAuth(r, cfg.NodeAPIKey) {
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}
		nodeType := "mobile"
		if strings.ToLower(strings.TrimSpace(r.URL.Query().Get("node_type"))) == "pc" {
			nodeType = "pc"
		}
		rawConn, err := wsUpgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		nodeConn := &wsClientConn{conn: rawConn}

		hub.mu.Lock()
		hub.nodeSockets[nodeType][nodeConn] = struct{}{}
		queued := hub.pendingCommands[nodeType]
		hub.pendingCommands[nodeType] = make([]ReplyCommand, 0)
		hub.mu.Unlock()

		hub.RecordHeartbeat(nodeType)
		hub.BroadcastStatusIfChanged(true)
		log.Printf("[Node WS] Node [%s] connected", nodeType)

		for _, cmd := range queued {
			_ = nodeConn.writeJSON(map[string]any{"type": "reply_command", "data": cmd})
		}

		defer func() {
			hub.mu.Lock()
			delete(hub.nodeSockets[nodeType], nodeConn)
			remaining := len(hub.nodeSockets[nodeType])
			if nodeType == "pc" && remaining == 0 {
				hub.lastPCHeartbeat = time.Time{}
			}
			hub.mu.Unlock()
			hub.BroadcastStatusIfChanged(true)
			_ = rawConn.Close()
			log.Printf("[Node WS] Node [%s] disconnected", nodeType)
		}()

		for {
			var pkt map[string]any
			if err := rawConn.ReadJSON(&pkt); err != nil {
				break
			}
			msgType, _ := pkt["type"].(string)
			switch msgType {
			case "heartbeat", "ping":
				snap := hub.RecordHeartbeat(nodeType)
				hub.BroadcastStatusIfChanged(false)
				_ = nodeConn.writeJSON(map[string]any{"type": "heartbeat_ack", "data": snap})
			case "uplink_message":
				dataMap, _ := pkt["data"].(map[string]any)
				if dataMap == nil {
					dataMap = pkt
				}
				sender, _ := dataMap["sender"].(string)
				content, _ := dataMap["content"].(string)
				isGroup, _ := dataMap["is_group"].(bool)
				roomName, _ := dataMap["room_name"].(string)
				res := handleUplinkMessage(sender, content, time.Now().Unix(), nodeType, isGroup, roomName)
				_ = nodeConn.writeJSON(map[string]any{"type": "uplink_ack", "data": res})
			}
		}
	})

	log.Printf("WeChat Relay Hub (Go Native Edition) listening on http://%s (Memory < 15MB, No VMware/Docker)", cfg.ListenAddr)
	if err := http.ListenAndServe(cfg.ListenAddr, mux); err != nil {
		log.Fatalf("Server exited: %v", err)
	}
}
