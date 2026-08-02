@echo off
REM ===========================================================================
REM deploy.bat - one-shot setup for the MCP Memory server (Windows)
REM Wraps deploy.ps1 with -ExecutionPolicy Bypass so users do not need to
REM relax their machine-wide PowerShell policy.
REM
REM Default behaviour (no args): create venv (auto-detects Python) +
REM install runtime deps (offline vendor preferred, online fallback) +
REM verify imports.
REM
REM Usage:
REM   deploy.bat                                   one-shot setup (recommended)
REM   deploy.bat -ForceRecreate                    rebuild venv from scratch
REM   deploy.bat -PythonExe "C:\Py311\python.exe"  pin a specific interpreter
REM   deploy.bat -InstallDevDeps                   also install dev/test deps
REM   deploy.bat -RegisterVSCode                   also write .vscode/mcp.json
REM   deploy.bat -SkipInstall                      only ensure venv exists
REM   deploy.bat -NoVerify                         skip the import sanity check
REM   deploy.bat -DownloadModel                    bootstrap embedding model
REM                                                (default preset bge-small-zh-v1.5;
REM                                                 prefers vendor\models\ then HTTP)
REM   deploy.bat -DownloadModel -ModelOffline      forbid HTTP; require vendor copy
REM   deploy.bat -DownloadModel -ModelOptional     do not fail deploy on download error
REM   deploy.bat -DownloadModel -ModelPreset paraphrase-multilingual-MiniLM-L12-v2
REM   deploy.bat -DownloadModel -ModelVendorDir "D:\offline\models"
REM ===========================================================================
setlocal
set "SCRIPT_DIR=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%deploy.ps1" %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
