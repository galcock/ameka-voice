@echo off
title Ameka Voice installer
echo.
echo   Installing Ameka Voice. This window will explain what it is doing.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://ameka.ai/install.ps1 | iex"
if errorlevel 1 (
  echo.
  echo   Something went wrong above. The log is in %USERPROFILE%\.local\state\ameka\ameka.log
  echo.
  pause
)
