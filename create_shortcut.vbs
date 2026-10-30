Set shell = CreateObject("WScript.Shell")
Set filesystem = CreateObject("Scripting.FileSystemObject")

If WScript.Arguments.Count <> 2 Then
    WScript.Echo "Informe o caminho do executavel e do icone."
    WScript.Quit 1
End If

targetPath = filesystem.GetAbsolutePathName(WScript.Arguments(0))
iconPath = filesystem.GetAbsolutePathName(WScript.Arguments(1))
If Not filesystem.FileExists(targetPath) Then
    WScript.Echo "Executavel nao encontrado: " & targetPath
    WScript.Quit 1
End If
If Not filesystem.FileExists(iconPath) Then
    WScript.Echo "Icone nao encontrado: " & iconPath
    WScript.Quit 1
End If

desktopPath = shell.SpecialFolders("Desktop")
shortcutPath = filesystem.BuildPath(desktopPath, "ControleMed.lnk")
Set shortcut = shell.CreateShortcut(shortcutPath)
shortcut.TargetPath = targetPath
shortcut.WorkingDirectory = filesystem.GetParentFolderName(targetPath)
shortcut.Description = "ControleMed"
shortcut.IconLocation = iconPath & ",0"
shortcut.Save

If Not filesystem.FileExists(shortcutPath) Then
    WScript.Echo "Nao foi possivel criar o atalho: " & shortcutPath
    WScript.Quit 1
End If

WScript.Echo "Atalho criado: " & shortcutPath