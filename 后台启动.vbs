' 双击 = 无窗口后台启动监控(pythonw,不闪任何窗口)
' 日志见同目录 watch.log;停止请双击 停止监控.bat
CreateObject("WScript.Shell").Run _
    """G:\nixang\huanjing\pythonw.exe"" ""G:\zcode\wokea\tg-channel-watch\watch.py""", 0, False
