' Abre TISMA Code sin la ventana de la consola. La consola existe (oculta): el motor la comparte y asi se le puede
' pedir que se cierre en orden. Al cerrar la ventana de TISMA Code, todo termina.
Set fso = CreateObject("Scripting.FileSystemObject")
raiz = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.Run """" & raiz & "\TISMA Code.cmd""", 0, False
