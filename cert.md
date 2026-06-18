PYRO TRICHY calling https://smpyrogateway.bsnl.co.in/frc/api/v1
PYRO TRICHY should resolve domain smpyrogateway.bsnl.co.in to IP: 10.201.222.67 by configuring in /etc/hosts file.
Note: Avoid http proxy for this url/IP (smpyrogateway.bsnl.co.in /10.201.222.67) either by exporting no_proxy in server profile or in API call.
PYRO TRICHY After resolution https://smpyrogateway.bsnl.co.in/frc/api/v1 will be sent to SM Server at SDC
SDC SM Server (10.201.222.67)
https://smpyrogateway.bsnl.co.in/api
Creation of Trusted Certificate(SSL) at SM Server: 
1. Create own internal Certificate Authority (internalCA.crt).
		    2. Generate Server certificate for https://smpyrogateway.bsnl.co.in/api
		    3. Install certificate on SM Server
		    4. Import the CA certificate (internalCA.crt)onto Pyro Server.

mkdir -p ~/frc/nginx/ssl && cd ~/frc/nginx/ssl

# Step 1 — Create internal CA
openssl genrsa -out internalCA.key 4096
MSYS_NO_PATHCONV=1 openssl req -x509 -new -nodes \
  -key internalCA.key \
  -sha256 -days 3650 \
  -subj "/CN=BSNL-SM-InternalCA" \
  -out internalCA.crt

# Step 2 — Generate server key + CSR
openssl genrsa -out smpyrogateway.bsnl.co.in.key 2048
MSYS_NO_PATHCONV=1 openssl req -new \
  -key smpyrogateway.bsnl.co.in.key \
  -subj "/CN=smpyrogateway.bsnl.co.in" \
  -out smpyrogateway.bsnl.co.in.csr

# Step 3 — Sign with internal CA
openssl x509 -req \
  -in smpyrogateway.bsnl.co.in.csr \
  -CA internalCA.crt -CAkey internalCA.key \
  -CAcreateserial \
  -days 825 -sha256 \
  -out smpyrogateway.bsnl.co.in.crt

Send internalCA.crt to Pyro
# On Pyro server 
cp internalCA.crt /usr/local/share/ca-certificates/
update-ca-certificates