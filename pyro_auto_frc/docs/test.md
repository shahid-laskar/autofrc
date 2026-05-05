 Created /D:/PROJECT/Sanchar_mitra/pyro_auto_frc/test_single_recharge.py.

  It does:

  1. Pyro authentication
  2. One recharge request
  3. Waits 45 seconds by default
  4. Calls transaction-status using returned data.transactionId
  5. Does not import or touch Oracle/Postgres

  Run it like this:

  python test_single_recharge.py --dealer-msisdn 9XXXXXXXXX --dest-msisdn 9XXXXXXXXX --amount 10 --mpin 1234 --confirm-real-recharge

  Optional custom client transaction ID:

  python test_single_recharge.py --dealer-msisdn 9XXXXXXXXX --dest-msisdn 9XXXXXXXXX --amount 10 --mpin 1234 --client-txn-id TEST001 --confirm-real-recharge

  Check status immediately instead of waiting:

  python test_single_recharge.py --dealer-msisdn 9XXXXXXXXX --dest-msisdn 9XXXXXXXXX --amount 10 --mpin 1234 --status-wait-seconds 0 --confirm-real-recharge

  Show full tokens:

  python test_single_recharge.py --dealer-msisdn 9XXXXXXXXX --dest-msisdn 9XXXXXXXXX --amount 10 --mpin 1234 --show-tokens --confirm-real-recharge

  I added the required --confirm-real-recharge flag because this can trigger an actual recharge. Syntax check passes with python -m py_compile test_single_recharge.py.

python test_single_recharge.py --dealer-msisdn 8903696189 --dest-msisdn 9402372619 --amount 1 --mpin 147258 --show-tokens --confirm-real-recharge --client-txn-id STEST001

 Use this env flag:

  ENABLE_SCHEDULER=false

  With that, the app will still start normally:

  - FastAPI app starts
  - DB pools initialize
  - Pyro authentication runs
  - Routes are available
  - Scheduler jobs do not start

  Run locally in PowerShell without editing .env:

  $env:ENABLE_SCHEDULER="false"
  uvicorn main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips "*"

  Then test:

  curl http://127.0.0.1:8000/health
  curl http://127.0.0.1:8000/token-status

  Swagger UI:

  http://127.0.0.1:8000/docs

  Important: scheduler is disabled, but manual admin endpoints still work. So do not call these unless you intentionally want DB writes / recharge processing:

  POST /admin/trigger-batch-population
  POST /admin/trigger-recharge
  POST /admin/trigger-status-check

  I also fixed startup blockers while adding this:
