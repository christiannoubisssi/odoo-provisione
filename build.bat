@echo off
title Construction de OdooProvisioner.exe
echo.
echo ======================================
echo   Construction de OdooProvisioner
echo ======================================
echo.

echo [1/3] Installation des dependances...
pip install customtkinter paramiko pyinstaller
echo.

echo [2/3] Construction de l'executable...
pyinstaller --onefile --windowed ^
  --name "OdooProvisioner" ^
  --add-data "%LOCALAPPDATA%\Programs\Python\Python3*\Lib\site-packages\customtkinter;customtkinter" ^
  main.py
echo.

echo [3/3] Copie des fichiers necessaires...
if not exist "dist\" mkdir dist
copy requirements.txt dist\ >nul 2>&1

echo.
echo ======================================
echo  TERMINE ! Executable dans : dist\
echo  Fichier : OdooProvisioner.exe
echo ======================================
pause