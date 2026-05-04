# Prepaid FRC Recharge API

**Document Number:** SCMPREPAID0425  
**Version:** 1.0  
**Date:** 04th April 2025

---

> © 2025 Pyro Holdings Pvt. Ltd. All rights reserved. No part of this document may be reproduced or transmitted in any form or by any means, electronic or otherwise, including photocopying, reprinting or recording, for any purpose, without the express written permission of Pyro Holdings Pvt. Ltd.

---

## 1. Introduction

This document gives the detailed information on the API to integrate with Pyro's to Perform Prepaid FRC Transactions.

---

## 2. Application Protocol Interface

System uses the AAA (Authentication, Authorization & Accounting) methodology for each request. Pyro Server first authenticates and authorizes the request from the requester. If authentication and authorization are successful, then it will process the request further, and send response as synchronization in the same requester URL.

The complete process will be accounted in the system for further reference. The following section gives information about all the services that support request & response formats.

Minor changes can be done based on the requirement in case there is no major code change.

---

## 3. Authentication API

Below API is used to generate `sessionToken` and `accessToken` after successful validation of `loginId` and `password`.

- **sessionToken:** Token generated using below API will be useful in further APIs and is valid for **24 hours**.
- **accessToken:** Token generated using below API will be useful in further APIs and is valid for **15 minutes**.

### Request Parameters

| Parameter | Description |
|-----------|-------------|
| `apiKey` | Unique key, will be shared by Pyro |
| `loginId` | Vendor Wallet/Mobile Number |
| `password` | MPIN of the Vendor Wallet |

### Response Parameters

| Parameter | Description |
|-----------|-------------|
| `statusCode` | Status code for Authentication |
| `status` | Status of the Authentication request |
| `message` | Message |
| `sessionToken` | Token generated and will be active for 24 hours |
| `accessToken` | Access token generated and will be active for 15 minutes |
| `userName` | Name of the Vendor |

### Request / Response Format

**URL:** `https://IP:PORT/auth-api/authentication`

**Header Parameters:**
```
apiKey: xxxxxxxxxxx
```

**Request Body:**
```json
{
  "loginId": "xxxxxxxxxxx",
  "password": "xxxx"
}
```

**Response Body:**
```json
{
  "statusCode": 2000,
  "status": "SUCCESS",
  "message": "authentication",
  "data": {
    "sessionToken": "xxxxxxxxxxx",
    "accessToken": "xxxxxxxxxxx",
    "userName": "BSNL1"
  }
}
```

---

## 4. Refresh Access Token API

As the `accessToken` is valid for 15 minutes, it can be refreshed every 15 minutes using this API.

### Request Parameters

| Parameter | Description |
|-----------|-------------|
| `apiKey` | Unique key, will be shared by Pyro |
| `sessionToken` | Token generated through Authentication API |
| `accessToken` | Token generated through Authentication API |

### Response Parameters

| Parameter | Description |
|-----------|-------------|
| `statusCode` | Status code for Authentication |
| `status` | Status of the Authentication request |
| `message` | Message |
| `accessToken` | Access token generated and will be active for 15 minutes |

### Request / Response Format

**URL:** `https://IP:PORT/auth-api/refresh-access-token`

**Header Parameters:**
```
apiKey: xxxxxxxxxxx
sessionToken: xxxxxxxxxxx
accessToken: xxxxxxxxxxx
```

**Request Body:** No Parameters

**Response Body:**
```json
{
  "statusCode": 2000,
  "status": "SUCCESS",
  "message": "refresh-access-token",
  "data": {
    "accessToken": "xxxxxxxxxxx"
  }
}
```

---

## 5. Recharge

### Request Parameters

| Parameter | Data Type | Size | Description |
|-----------|-----------|------|-------------|
| `dealerMsisdn` | String | 10 | Source/Vendor Msisdn |
| `amount` | Double | 10 | Amount |
| `destMsisdn` | String | 10 | Subscriber Msisdn |
| `clientTxnId` | String | 5 to 15 | Vendor Transaction ID |
| `mpin` | String | 6 | MPIN of Vendor Msisdn |

### Response Parameters

| Parameter | Description |
|-----------|-------------|
| `statusCode` | Status code for Authentication |
| `status` | Status of the Authentication request |
| `message` | Message |
| `clientTxnId` | Vendor Transaction ID |
| `transactionId` | Pyro Transaction ID |
| `serviceType` | Type of Transaction |
| `destMsisdn` | Subscriber Msisdn |
| `amount` | Denomination |

### Request / Response Format

**URL:** `https://IP:PORT/epin-vendor-api/recharge`

**Header Parameters:**
```
apiKey: xxxxxxxxxxx
sessionToken: optional
accessToken: xxxxxxxxxxx
actionToken: xxxxxxxxxxx
```

**Request Body:**
```json
{
  "dealerMsisdn": "9XXXXXXXXX",
  "destMsisdn": "9XXXXXXXXX",
  "amount": 199,
  "clientTxnId": "1234213",
  "mpin": "xxxxxx"
}
```

**Response Body:**
```json
{
  "statusCode": 2002,
  "status": "In Process",
  "message": "Request is Registered",
  "data": {
    "clientTxnId": "123456",
    "transactionId": 1234,
    "serviceType": "TOPUP",
    "destMsisdn": "9440123456",
    "amount": 199
  }
}
```

---

## 5. Recharge Callback

The vendor must share their callback URL with Pyro. Pyro will POST the final recharge result to this URL asynchronously.

### Callback Request Parameters

| Parameter | Description |
|-----------|-------------|
| `statusCode` | Status code for transaction |
| `status` | Status of the transaction |
| `message` | Message |
| `clientTxnId` | Vendor Transaction ID |
| `transactionId` | Pyro Transaction ID |
| `serviceType` | Type of Transaction |
| `destMsisdn` | Subscriber Msisdn |
| `circle` | Circle of the subscriber |
| `amount` | Denomination |
| `dealerBalanceBefore` | Dealer balance before transaction |
| `dealerBalanceAfter` | Dealer balance after transaction |

**URL:** Vendor needs to share the callback URL to receive the Recharge API result.

**Callback Request Body (POST from Pyro):**
```json
{
  "statusCode": 2000,
  "status": "SUCCESS",
  "message": "Recharge successful",
  "data": {
    "clientTxnId": "123456",
    "transactionId": 1234,
    "serviceType": "TOPUP",
    "destMsisdn": "9440123456",
    "circle": "Telangana",
    "amount": 199,
    "status": "SUCCESS",
    "dealerBalanceBefore": 2800.0,
    "dealerBalanceAfter": 2250.0
  }
}
```

---

## 7. Generate Action Token API

`actionToken`: Token generated using the below API is useful in further APIs related to recharge and topup where balance movement is involved. It is valid for **1 minute** and will expire after a **single use**.

### Response Parameters

| Parameter | Description |
|-----------|-------------|
| `statusCode` | Status code for Action Token API |
| `status` | Status of the Action Token request |
| `message` | Message |
| `accessToken` | Access token generated and will be active for 15 minutes |
| `actionToken` | Action token generated and will be active for 1 minute |

### Request / Response Format

**URL:** `https://IP:PORT/auth-api/generate-action-token`

**Header Parameters:**
```
apiKey: xxxxxxxxxxx
sessionToken: optional
accessToken: xxxxxxxxxxx
```

**Request Body:** No Parameters

**Response Body:**
```json
{
  "statusCode": 2000,
  "status": "SUCCESS",
  "message": "generate-action-token",
  "data": {
    "accessToken": "xxxxxxxxxxx",
    "actionToken": "xxxxxxxxxxx"
  }
}
```

---

## 8. Transaction Status Check

This API is used to check the status of the Prepaid transaction. It needs to be initiated in case of no callback response on any transaction, and must only be called after a **minimum of 45 seconds** after the transaction.

### Request Parameters

| Parameter | Description |
|-----------|-------------|
| `transactionId` | Pyro Transaction ID |
| `clientTxnId` | Vendor Transaction ID |

### Response Parameters

| Parameter | Description |
|-----------|-------------|
| `statusCode` | Status code for Authentication |
| `status` | Status of the Authentication request |
| `message` | Message |
| `clientTxnId` | Vendor Transaction ID |
| `transactionId` | Pyro Transaction ID |
| `serviceType` | Type of Transaction |
| `destMsisdn` | Subscriber Msisdn |
| `circle` | Circle of the subscriber |
| `amount` | Denomination |
| `dealerBalanceBefore` | Balance before transaction of the dealer |
| `dealerBalanceAfter` | Balance after transaction of the dealer |

### Request / Response Format

**URL:** `https://IP:PORT/epin-vendor-api/transaction-status`

**Header Parameters:**
```
apiKey: xxxxxxxxxxx
sessionToken: optional
accessToken: xxxxxxxxxxx
```

**Request Body:**
```json
{
  "transactionId": "12345",
  "clientTxnId": "123456"
}
```

**Response Body:**
```json
{
  "statusCode": 2000,
  "status": "SUCCESS",
  "message": "Recharge successful",
  "data": {
    "clientTxnId": "123456",
    "transactionId": "1234",
    "serviceType": "TOPUP",
    "destMsisdn": "9440123456",
    "circle": "Telangana",
    "amount": 199,
    "status": "SUCCESS",
    "dealerBalanceBefore": 2800.0,
    "dealerBalanceAfter": 2250.0
  }
}
```

---

## 9. Response Codes

| Status Code | Condition / Scenario | Description |
|-------------|----------------------|-------------|
| `2000` | Success | SUCCESS |
| `2002` | Recharge Request Registered Successfully | Request Registered Successfully |
| `405` | Not enough stock to do recharge | Insufficient stock |
| `406` | Vendor/retailer account has been suspended or inactive | Account is suspended or inactive |
| `415` | 15 min block out for same amount/number | Same number with same amount |
| `500` | Pyro Internal Application issue while processing | Currently system is not responding, please try again later |
| `505` | Request format not valid/incorrect | The format of request is not valid |
| `506` | Invalid Token | Invalid Token |
| `901` | Status check when there is no transaction found | No Transaction found |
| `902` | Failed Transaction on Status Check | Status of the Transaction is Failed |
| `5000` | Diameter Failures from IN | Diameter Error |
| `5001` | Username is Incorrect | You are not authorized to use this service |
| `5002` | Wrong Password | You are not authorized to use this service |
| `5006` | Number not found in Pyro System | NUMBER NOT FOUND |
| `5007` | Source number not found or not active | Invalid Source Number |
| `5011` | Wrong Denomination | Invalid Denomination |
| `5012` | Invalid MPIN | Invalid MPIN |
| `5030` | Service class not found in card group | Data validation failed |

---

> **NOTE:** Request and Response Parameters are encrypted in **3DES**. Final response on callback will be decrypted.
