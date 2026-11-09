Set WshShell = CreateObject("WScript.Shell")
strAppDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\.."
WshShell.CurrentDirectory = strAppDir
' 0 表示完全隐藏黑框窗口后台运行，False 表示不阻塞
WshShell.Run """" & strAppDir & "\wechat-relay-hub.exe"" -config """ & strAppDir & "\config.json""", 0, False
