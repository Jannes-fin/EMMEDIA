@echo off
echo EM Media — Whop Research Agent Setup
echo =====================================

echo Installing dependencies...
pip install -r requirements.txt

echo.
echo Creating .env file...
if not exist .env (
    copy .env.example .env
    echo .env created. Please fill in your API keys.
) else (
    echo .env already exists, skipping.
)

echo.
echo Setup complete!
echo.
echo Next steps:
echo 1. Edit .env and add your ANTHROPIC_API_KEY and DISCORD_WEBHOOK_URL
echo 2. Edit keywords.json to adjust search keywords
echo 3. Run: python agent.py          (scheduled daily at 9:00 AM)
echo    Run: python agent.py --now    (run immediately for testing)
echo.
pause
