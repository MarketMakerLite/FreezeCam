@echo off
REM Navigate to the project directory
cd /d "%USERPROFILE%\Desktop\FreezeCam"

REM Activate the virtual environment
call env\Scripts\activate

REM Start the Flask app
python Flask_OBS_Lag_Cam-socketio_share.py

REM Keep the window open after execution
pause