## 1. The complete data flow — step by step

### Phase 1 — Batch Population (runs every 1 hour , customisable via env:SCHEDULER_BATCH_POPULATION_INTERVAL_MINUTES=30)

```
STEP 1: Query Oracle BCD table
        Filter: ACTIVATION_STATUS ='C' ← subscriber is activated
                HLR_FINAL_ACT_DATE IS NOT NULL   ← subscriber is activated
                FRC_FLOW_STATUS = 'NP'            ← not yet processed
                FRC_REQID IS NULL                 ← no prior FRC request
        Result: list of GSM numbers + CAF serial numbers

STEP 2: For each GSM number, query Postgres cos_bcd, cos_bcd_dkyc
        Check: frc_plan_name, frc_plan_code, frc_category_code,
               frc_ctopup_number, frc_ctopup_number_mpin are ALL non-null
        If any is null → skip (this subscriber doesn't need FRC)
        in cos_bcd_dkyc, there is no plan_code ?? we are taking it from plan table
        Also joins: ctop_master   → get vendorid, vendormsisdn
                    frc_plan_table → get frcamt (the amount to recharge)

STEP 3: Encrypt the MPIN from cos_bcd before saving

STEP 4: Insert one row per subscriber into Postgres frc_pyro_request_data
        push_flag = 'N'  (Not yet pushed to Pyro)

STEP 5: Write back to Oracle BCD:
        FRC_FLOW_STATUS = 'RQ'  (Request Queued)
        FRC_REQID = reqid       (links BCD to our new row)
        ← This prevents the same subscriber being picked up tomorrow
```

### Phase 2 — Recharge Dispatch (runs every 30 minutes, env: )

```
STEP 6: Query Postgres frc_pyro_request_data
        Filter: push_flag IN ('N', 'E')  ← new or retry rows

STEP 7: For each row:
        a. Get a fresh actionToken from Pyro API
           (actionToken is single-use, expires in 1 minute)
        b. Build recharge request payload:
           { dealerMsisdn, destMsisdn, amount, clientTxnId, mpin }
        c. Encrypt the entire JSON payload with 3DES
        d. POST to Pyro /epin-vendor-api/recharge
        e. Decrypt the response

STEP 8: Handle response:
        statusCode 2002 → "Registered" — Pyro accepted the request
                          push_flag = 'P'  (Pushed, awaiting callback)
                          BCD FRC_FLOW_STATUS = 'W'  (Waiting)
        statusCode 5006/5007/etc → Data error — cannot retry
                          push_flag = 'F'  (Failed permanently)
                          BCD FRC_FLOW_STATUS = 'ID' (Invalid Data)
        statusCode 500 → Pyro internal error — retry later
                          push_flag = 'E'  (Error, will retry)
```

### Phase 3 — Result Handling (two paths)

**Path A: Callback (preferred — Pyro calls us)**
```
STEP 9a: Pyro POSTs to https://smpyrogateway.bsnl.co.in/api/callback/recharge
         Body contains: transactionId, statusCode, dealerBalanceAfter, etc.
         Our service looks up the row using transactionId → pyro_trans_id

STEP 10a: Update Postgres frc_pyro_request_data:
          If statusCode 2000 (SUCCESS): push_flag = 'Y'
          If anything else:             push_flag = 'F'

STEP 11a: Update Oracle BCD:
          If success: FRC_FLOW_STATUS = 'P'  (Processed)
          If failure: FRC_FLOW_STATUS = 'F'  (Failed)
```

**Path B: Status Check / Watchdog (fallback — if no callback after 2 minutes)**
```
STEP 9b: Every 5 minutes, check for rows where:
         push_flag = 'P' AND pushed more than 2 minutes ago

STEP 10b: POST to Pyro /epin-vendor-api/transaction-status
          Response tells us if the recharge succeeded or failed

STEP 11b: Same DB updates as Path A
          Extra: BCD FRC_FLOW_STATUS = 'NR' on first check (No Response received)
```