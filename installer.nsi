; -------------------------------------------------------------------
; Noesis PDF Cloner — Windows Installer (NSIS 3.x)
;
; Usage:
;   makensis installer.nsi
;   makensis /DVERSION=0.1.0 installer.nsi
;
; Produces: NoesisPDFCloner-${VERSION}-setup.exe
; -------------------------------------------------------------------

!include "MUI2.nsh"
!include "FileFunc.nsh"

; -------------------------------------------------------------------
; Configurable defines (override with /D on the command line)
; -------------------------------------------------------------------
!ifndef VERSION
  !define VERSION "0.1.0"
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

; -------------------------------------------------------------------
; .onInit — language selection
; -------------------------------------------------------------------
Function .onInit
  !insertmacro MUI_LANGDLL_DISPLAY
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
; Uninstaller
; -------------------------------------------------------------------
Section "Uninstall"
  ; Remove application files
  Delete "$INSTDIR\${APP_EXE}"
  Delete "$INSTDIR\setup_engine.ps1"
  Delete "$INSTDIR\uninst.exe"
  RMDir  "$INSTDIR"

  ; Remove shortcuts
  Delete "$DESKTOP\${PRODUCT_NAME}.lnk"
  Delete "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk"
  Delete "$SMPROGRAMS\${PRODUCT_NAME}\Uninstall.lnk"
  RMDir  "$SMPROGRAMS\${PRODUCT_NAME}"

  ; Remove registry entries
  DeleteRegKey HKLM "${PRODUCT_REGKEY}"
  DeleteRegKey HKLM "Software\${PRODUCT_NAME}"
SectionEnd