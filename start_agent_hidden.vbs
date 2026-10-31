' Запуск ассистента без окна. Если процесс упал, через 10 секунд стартует снова.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
Do
  sh.Run "cmd /c start_agent.bat", 0, True
  WScript.Sleep 10000
Loop
