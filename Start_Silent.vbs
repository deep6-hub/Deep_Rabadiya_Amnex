Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder
shell.Run Chr(34) & folder & "\\start_silent.bat" & Chr(34), 0, False
WScript.Sleep 1800
shell.Run "http://127.0.0.1:8000", 1, False
