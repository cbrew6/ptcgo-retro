' Launches the local server with no console window.
'
' The scheduled task runs as the logged-on user (S4U, which would run in
' session 0 with no window at all, needs admin rights we do not have), so the
' task's own cmd.exe would otherwise pop a terminal onto the desktop.
' WScript.Shell.Run with a window style of 0 starts it hidden instead.
'
' bWaitOnReturn is True on purpose: this script stays alive for as long as the
' server does, so it remains the task's foreground process. Returning early
' would let Task Scheduler treat the task as finished and reap the server.

Dim shell, repo, python, command
Set shell = CreateObject("WScript.Shell")

repo = "C:\Users\cbrew\AppData\Roaming\Pok" & ChrW(233) & "mon Trading Card Game Online\ptcgo-local"
python = "C:\Users\cbrew\AppData\Local\Programs\Python\Python312\python.exe"

shell.CurrentDirectory = repo
command = "cmd /c """"" & python & """ server.py >> server.out.log 2>&1"""

shell.Run command, 0, True
