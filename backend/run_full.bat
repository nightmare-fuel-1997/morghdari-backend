@echo off
cd /d M:\projects\poultry_the_main_by_ai\backend
set DATABASE_URL=postgres://morghdari_dev:dev_password_only@localhost:5432/morghdari_dev
set PYTHONUNBUFFERED=1
.venv\Scripts\python.exe -m pytest --cov=apps.alerts --cov-report=term > pytest_full.log 2>&1
echo DONE_PYTEST >> pytest_full.log