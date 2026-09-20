' CC Relay 静默启动器 - 双击无黑框运行并打开 UI
Set ws = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' 获取当前目录
currentDir = fso.GetParentFolderName(WScript.ScriptFullName)
ws.CurrentDirectory = currentDir

' 优先检测打包生成的 cc-relay.exe
distExe = currentDir & "\dist\cc-relay.exe"
localExe = currentDir & "\cc-relay.exe"

If fso.FileExists(localExe) Then
    ws.Run """" & localExe & """", 0, False
ElseIf fso.FileExists(distExe) Then
    ws.Run """" & distExe & """", 0, False
Else
    ' 若未编译 exe，直接使用本地 pythonw 静默拉起主启动器
    ws.Run "pythonw """ & currentDir & "\main_launcher.py""", 0, False
End If
