' Launch alert.ps1 with no window. The Gamma_X Alert task runs on the desktop
' every 15 minutes through the session; started as powershell.exe directly,
' each run would flash a console window in front of whatever you are doing.
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & here & "\alert.ps1""", 0, False
