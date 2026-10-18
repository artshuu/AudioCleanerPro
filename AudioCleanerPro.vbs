Option Explicit

Dim fso, shell, baseDir, pythonw, appScript

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = baseDir & "\venv\Scripts\pythonw.exe"
appScript = baseDir & "\app.py"

If Not fso.FileExists(appScript) Then
    MsgBox "app.py was not found next to this file.", vbCritical, "Audio Cleaner Pro"
    WScript.Quit 1
End If

If Not fso.FileExists(pythonw) Then
    MsgBox "Environment is not installed." & vbCrLf & vbCrLf & _
           "Run install.bat first, then try again.", _
           vbCritical, "Audio Cleaner Pro"
    WScript.Quit 1
End If

shell.CurrentDirectory = baseDir
shell.Run """" & pythonw & """ """ & appScript & """", 0, False
