' Hidden launcher of the assistant (scheduled task "Consigliere", see install_autostart.bat).
' The restart policy lives in agent\supervisor.py: backoff 10s, 30s, 60s, 5 min; stop with a log entry and
' a message box on exit codes 2 (no settings), 3 (no Telegram session). This file only starts it.
' Message texts below are ASCII on purpose: wscript reads this file in the ANSI code page.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = root
If Not fso.FileExists(root & "\.venv\Scripts\python.exe") Then
  MsgBox "Consigliere: .venv not found. Run install.bat first.", 48, "Consigliere"
  WScript.Quit 4
End If
rc = sh.Run("cmd /c start_agent.bat --supervised", 0, True)
' codes 2 and 3: the supervisor has already shown its own message and written the log
If rc <> 0 And rc <> 2 And rc <> 3 Then
  MsgBox "Consigliere autostart stopped unexpectedly (exit code " & rc & "). See data\assistant.log", 48, "Consigliere"
End If
WScript.Quit rc
