@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title AutoValor Pro Colombia
set "ROOT=%~dp0"
set "PYEXE=%ROOT%.venv\Scripts\python.exe"

echo ==============================================================
echo   AUTOVALOR PRO COLOMBIA - INICIO LOCAL
echo ==============================================================
echo.
echo Este proceso puede tardar unos minutos la primera vez.
echo No cierres esta ventana mientras uses la aplicacion.
echo.

if not exist "%PYEXE%" (
    echo [1/4] Buscando Python instalado...
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 --version >nul 2>nul
        if errorlevel 1 goto PYTHON_MISSING
        echo Creando entorno virtual con Python Launcher...
        py -3 -m venv "%ROOT%.venv"
        if errorlevel 1 goto VENV_ERROR
    ) else (
        where python >nul 2>nul
        if errorlevel 1 goto PYTHON_MISSING
        python --version
        echo Creando entorno virtual con Python...
        python -m venv "%ROOT%.venv"
        if errorlevel 1 goto VENV_ERROR
    )
)

if not exist "%PYEXE%" goto VENV_ERROR

echo.
echo [2/4] Instalando o comprobando dependencias...
"%PYEXE%" -m pip install --disable-pip-version-check -r "%ROOT%requirements.txt"
if errorlevel 1 goto PIP_ERROR

echo.
echo [3/4] Preparando la configuracion local...
"%PYEXE%" "%ROOT%scripts\setup_env.py"
if errorlevel 1 goto CONFIG_ERROR

echo.
echo [4/4] Comprobando la base de datos...
"%PYEXE%" "%ROOT%scripts\init_db.py"
if errorlevel 1 goto DB_ERROR

echo.
echo El servidor va a iniciarse en http://127.0.0.1:5000
 echo Cuando el servidor este listo, se abrira el navegador automaticamente.
echo Para detener la aplicacion, vuelve a esta ventana y presiona CTRL+C.
echo.

REM Abre el navegador unos segundos despues, mientras el servidor arranca.
start "" powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:5000'"
"%PYEXE%" "%ROOT%app.py"
set "APP_EXIT=%ERRORLEVEL%"
echo.
if not "%APP_EXIT%"=="0" (
    echo ERROR: el servidor termino con un codigo %APP_EXIT%.
    echo Toma una foto de los mensajes anteriores y enviala para revisar el problema.
) else (
    echo El servidor se ha detenido.
)
echo.
pause
exit /b %APP_EXIT%

:PYTHON_MISSING
echo ERROR: No se encontro Python 3 instalado o no esta disponible en PATH.
echo Instala Python 3.10 o superior desde https://www.python.org/downloads/windows/
echo Durante la instalacion marca la casilla "Add Python to PATH".
start "" "https://www.python.org/downloads/windows/"
echo Despues de instalar Python, reinicia el computador y ejecuta este archivo otra vez.
echo.
pause
exit /b 1

:VENV_ERROR
echo ERROR: No se pudo crear el entorno de Python.
echo Comprueba que Python 3.10 o superior este instalado y que la carpeta permita escritura.
echo.
pause
exit /b 1

:PIP_ERROR
echo ERROR: No se pudieron instalar las dependencias.
echo Comprueba tu conexion a Internet y que no haya un bloqueo del antivirus o del proxy.
echo No cierres esta ventana: toma una foto del error para poder corregirlo.
echo.
pause
exit /b 1

:CONFIG_ERROR
echo ERROR: No se pudo preparar la configuracion local.
echo Toma una foto de los mensajes anteriores y envia la imagen para revisar el problema.
echo.
pause
exit /b 1

:DB_ERROR
echo ERROR: No se pudo inicializar la base de datos.
echo Toma una foto de los mensajes anteriores y envia la imagen para revisar el problema.
echo.
pause
exit /b 1
