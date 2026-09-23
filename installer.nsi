; -------------------------------------------------------------------
; Noesis PDF Cloner — Windows Installer (NSIS 3.x)
;
; Usage:
;   makensis installer.nsi
;   makensis /DVERSION=0.1.3 installer.nsi
;
; Produces: NoesisPDFCloner-${VERSION}-setup.exe
; -------------------------------------------------------------------

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"
!include "nsDialogs.nsh"

; -------------------------------------------------------------------
; Configurable defines (override with /D on the command line)
; -------------------------------------------------------------------
!ifndef VERSION
  !define VERSION "0.1.3"
!endif

!define PRODUCT_NAME      "Noesis PDF Cloner"
!define PRODUCT_PUBLISHER "Noesis"
!define PRODUCT_WEB_SITE  "https://github.com/vigliafg/noesis-pdf-cloner"
!define PRODUCT_REGKEY    "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}"
!define APP_EXE           "NoesisPDFCloner.exe"

; -------------------------------------------------------------------
; General
; -------------------------------------------------------------------
Name             "${PRODUCT_NAME} ${VERSION}"
OutFile          "NoesisPDFCloner-${VERSION}-setup.exe"
InstallDir       "$PROGRAMFILES64\${PRODUCT_NAME}"
InstallDirRegKey HKLM "Software\${PRODUCT_NAME}" "InstallDir"
RequestExecutionLevel admin
SetCompressor    /SOLID lzma
BrandingText      " "

; -------------------------------------------------------------------
; Uninstaller options page (handle + scelte dell'utente)
; -------------------------------------------------------------------
Var UN_H_ALL            ; checkbox "rimuovi tutto"
Var UN_H_ENGINE         ; checkbox motore
Var UN_H_DATA           ; checkbox dati app
Var UN_H_UV             ; checkbox cache/dati uv
Var UN_OPT_ENGINE       ; 1 = rimuovi il motore
Var UN_OPT_DATA         ; 1 = rimuovi i dati dell'app
Var UN_OPT_UV           ; 1 = rimuovi cache/dati di uv

; -------------------------------------------------------------------
; MUI2 interface
; -------------------------------------------------------------------
!define MUI_ABORTWARNING
!define MUI_ICON   "assets\noesispdf.ico"
!define MUI_UNICON "assets\noesispdf.ico"

; -- Pages ----------------------------------------------------------
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
UninstPage custom un.OptionsPage un.OptionsPageLeave
!insertmacro MUI_UNPAGE_INSTFILES

; -- Languages ------------------------------------------------------
!insertmacro MUI_LANGUAGE "English"
!insertmacro MUI_LANGUAGE "Italian"

; -- Reserve files (faster startup) ---------------------------------
!insertmacro MUI_RESERVEFILE_LANGDLL

; -------------------------------------------------------------------
; Strings (Italian localisation)
; -------------------------------------------------------------------
LangString NAME_Desktop     ${LANG_ITALIAN} "Collegamento sul Desktop"
LangString NAME_StartMenu   ${LANG_ITALIAN} "Collegamento nel menu Start"
LangString NAME_App         ${LANG_ITALIAN} "${PRODUCT_NAME} (richiesto)"

LangString DESC_Desktop     ${LANG_ITALIAN} "Crea un'icona di collegamento sul Desktop."
LangString DESC_StartMenu   ${LANG_ITALIAN} "Crea un gruppo con i collegamenti nel menu Start."
LangString DESC_App         ${LANG_ITALIAN} "File principali dell'applicazione."

LangString NAME_Desktop     ${LANG_ENGLISH} "Desktop shortcut"
LangString NAME_StartMenu   ${LANG_ENGLISH} "Start Menu shortcut"
LangString NAME_App         ${LANG_ENGLISH} "${PRODUCT_NAME} (required)"

LangString DESC_Desktop     ${LANG_ENGLISH} "Create a shortcut icon on the Desktop."
LangString DESC_StartMenu   ${LANG_ENGLISH} "Create a shortcut group in the Start Menu."
LangString DESC_App         ${LANG_ENGLISH} "Core application files."

; -- Uninstaller options page --------------------------------------
LangString UN_TITLE    ${LANG_ITALIAN} "Cosa rimuovere"
LangString UN_SUBTITLE ${LANG_ITALIAN} "Scegli cosa eliminare dal sistema."
LangString UN_INTRO    ${LANG_ITALIAN} "Per non lasciare alcuna traccia del programma, lascia selezionate tutte le voci qui sotto."
LangString UN_ALL      ${LANG_ITALIAN} "Rimuovi tutto: nessuna traccia del programma sul sistema"
LangString UN_ENGINE   ${LANG_ITALIAN} "Motore di traduzione (.venv2, ~1,1 GB)"
LangString UN_DATA     ${LANG_ITALIAN} "Impostazioni, cache delle traduzioni e log dell'app"
LangString UN_UV       ${LANG_ITALIAN} "Cache e dati di uv (%LOCALAPPDATA%\uv, %APPDATA%\uv)"

LangString UN_TITLE    ${LANG_ENGLISH} "What to remove"
LangString UN_SUBTITLE ${LANG_ENGLISH} "Choose what to delete from your system."
LangString UN_INTRO    ${LANG_ENGLISH} "To leave no trace of the program, keep all the items below selected."
LangString UN_ALL      ${LANG_ENGLISH} "Remove everything: no trace of the program on the system"
LangString UN_ENGINE   ${LANG_ENGLISH} "Translation engine (.venv2, ~1.1 GB)"
LangString UN_DATA     ${LANG_ENGLISH} "App settings, translation cache and logs"
LangString UN_UV       ${LANG_ENGLISH} "uv cache and data (%LOCALAPPDATA%\uv, %APPDATA%\uv)"

; -------------------------------------------------------------------
; .onInit — language selection
; -------------------------------------------------------------------
Function .onInit
  !insertmacro MUI_LANGDLL_DISPLAY
FunctionEnd

; -------------------------------------------------------------------
; un.onInit — recupera la lingua scelta in installazione (per le pagine
; personalizzate dell'uninstaller) e prosegue se non disponibile.
; -------------------------------------------------------------------
Function un.onInit
  !insertmacro MUI_UNGETLANGUAGE
FunctionEnd

; -------------------------------------------------------------------
; Installer sections
; -------------------------------------------------------------------
Section "$(NAME_App)" SectionApp
  SectionIn RO              ; required — cannot be deselected
  SetOutPath "$INSTDIR"
  SetOverwrite on

  ; Copy the PyInstaller-built executable from dist/
  File "dist\${APP_EXE}"

  ; Script di setup del motore di clonazione (installa .venv2 accanto all'app).
  File "setup_engine.ps1"

  ; Store install directory in registry for upgrade detection
  WriteRegStr HKLM "Software\${PRODUCT_NAME}" "InstallDir" "$INSTDIR"
  WriteRegStr HKLM "Software\${PRODUCT_NAME}" "Version"    "${VERSION}"

  ; Write uninstaller
  WriteUninstaller "$INSTDIR\uninst.exe"

  ; Register with Windows Add/Remove Programs
  WriteRegStr   HKLM "${PRODUCT_REGKEY}" "DisplayName"     "${PRODUCT_NAME}"
  WriteRegStr   HKLM "${PRODUCT_REGKEY}" "UninstallString" '"$INSTDIR\uninst.exe"'
  WriteRegStr   HKLM "${PRODUCT_REGKEY}" "DisplayIcon"     '"$INSTDIR\${APP_EXE}"'
  WriteRegStr   HKLM "${PRODUCT_REGKEY}" "DisplayVersion"  "${VERSION}"
  WriteRegStr   HKLM "${PRODUCT_REGKEY}" "Publisher"       "${PRODUCT_PUBLISHER}"
  WriteRegStr   HKLM "${PRODUCT_REGKEY}" "URLInfoAbout"    "${PRODUCT_WEB_SITE}"
  WriteRegDWORD HKLM "${PRODUCT_REGKEY}" "EstimatedSize"   100000 ; ~100 MB
  WriteRegDWORD HKLM "${PRODUCT_REGKEY}" "NoModify"        1
  WriteRegDWORD HKLM "${PRODUCT_REGKEY}" "NoRepair"        1
SectionEnd

Section /o "$(NAME_Desktop)" SectionDesktop
  CreateShortCut "$DESKTOP\${PRODUCT_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
SectionEnd

Section "$(NAME_StartMenu)" SectionStartMenu
  CreateDirectory "$SMPROGRAMS\${PRODUCT_NAME}"
  CreateShortCut  "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
  CreateShortCut  "$SMPROGRAMS\${PRODUCT_NAME}\Uninstall.lnk"       "$INSTDIR\uninst.exe"     "" "$INSTDIR\uninst.exe"     0
SectionEnd

; -------------------------------------------------------------------
; Section descriptions (shown when hovering)
; -------------------------------------------------------------------
!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SectionApp}       $(DESC_App)
  !insertmacro MUI_DESCRIPTION_TEXT ${SectionDesktop}   $(DESC_Desktop)
  !insertmacro MUI_DESCRIPTION_TEXT ${SectionStartMenu} $(DESC_StartMenu)
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; -------------------------------------------------------------------
; Uninstaller — pagina "Cosa rimuovere"
; -------------------------------------------------------------------
Function un.OptionsPage
  !insertmacro MUI_HEADER_TEXT "$(UN_TITLE)" "$(UN_SUBTITLE)"
  nsDialogs::Create 1018
  Pop $0
  ${If} $0 == error
    Abort
  ${EndIf}

  ${NSD_CreateLabel} 0 0 100% 26u "$(UN_INTRO)"
  Pop $0

  ${NSD_CreateCheckbox} 0 30u 100% 12u "$(UN_ALL)"
  Pop $UN_H_ALL
  ${NSD_SetState} $UN_H_ALL ${BST_CHECKED}
  ${NSD_OnClick} $UN_H_ALL un.ToggleAll

  ${NSD_CreateCheckbox} 12u 46u 100% 12u "$(UN_ENGINE)"
  Pop $UN_H_ENGINE
  ${NSD_SetState} $UN_H_ENGINE ${BST_CHECKED}

  ${NSD_CreateCheckbox} 12u 60u 100% 12u "$(UN_DATA)"
  Pop $UN_H_DATA
  ${NSD_SetState} $UN_H_DATA ${BST_CHECKED}

  ${NSD_CreateCheckbox} 12u 74u 100% 12u "$(UN_UV)"
  Pop $UN_H_UV
  ${NSD_SetState} $UN_H_UV ${BST_CHECKED}

  nsDialogs::Show
FunctionEnd

Function un.ToggleAll
  ${NSD_GetState} $UN_H_ALL $0
  ${If} $0 == ${BST_CHECKED}
    ${NSD_SetState} $UN_H_ENGINE ${BST_CHECKED}
    ${NSD_SetState} $UN_H_DATA ${BST_CHECKED}
    ${NSD_SetState} $UN_H_UV ${BST_CHECKED}
    EnableWindow $UN_H_ENGINE 0
    EnableWindow $UN_H_DATA 0
    EnableWindow $UN_H_UV 0
  ${Else}
    ${NSD_SetState} $UN_H_ENGINE ${BST_UNCHECKED}
    ${NSD_SetState} $UN_H_DATA ${BST_UNCHECKED}
    ${NSD_SetState} $UN_H_UV ${BST_UNCHECKED}
    EnableWindow $UN_H_ENGINE 1
    EnableWindow $UN_H_DATA 1
    EnableWindow $UN_H_UV 1
  ${EndIf}
FunctionEnd

Function un.OptionsPageLeave
  ${NSD_GetState} $UN_H_ENGINE $UN_OPT_ENGINE
  ${NSD_GetState} $UN_H_DATA $UN_OPT_DATA
  ${NSD_GetState} $UN_H_UV $UN_OPT_UV
FunctionEnd

; -------------------------------------------------------------------
; Uninstaller
; -------------------------------------------------------------------
Section "Uninstall"
  ; File dell'app
  Delete "$INSTDIR\${APP_EXE}"
  Delete "$INSTDIR\setup_engine.ps1"
  Delete "$INSTDIR\uninst.exe"

  ; Motore di traduzione: accanto all'app e nella cartella dati per-utente.
  ${If} $UN_OPT_ENGINE == ${BST_CHECKED}
    RMDir /r "$INSTDIR\.venv2"
    RMDir /r "$APPDATA\noesis-pdf-cloner\engine"
  ${EndIf}

  ; Dati dell'app: impostazioni, chiave API, cache traduzioni, log.
  ${If} $UN_OPT_DATA == ${BST_CHECKED}
    RMDir /r "$APPDATA\noesis-pdf-cloner"
  ${EndIf}

  ; Cache e dati di uv (condivisi con altri eventuali usi di uv).
  ${If} $UN_OPT_UV == ${BST_CHECKED}
    RMDir /r "$LOCALAPPDATA\uv"
    RMDir /r "$APPDATA\uv"
  ${EndIf}

  ; Cartella dell'app: ricorsiva solo se può contenere il motore rimosso.
  ${If} $UN_OPT_ENGINE == ${BST_CHECKED}
    RMDir /r "$INSTDIR"
  ${Else}
    RMDir "$INSTDIR"
  ${EndIf}

  ; Collegamenti
  Delete "$DESKTOP\${PRODUCT_NAME}.lnk"
  Delete "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk"
  Delete "$SMPROGRAMS\${PRODUCT_NAME}\Uninstall.lnk"
  RMDir  "$SMPROGRAMS\${PRODUCT_NAME}"

  ; Registro
  DeleteRegKey HKLM "${PRODUCT_REGKEY}"
  DeleteRegKey HKLM "Software\${PRODUCT_NAME}"
SectionEnd