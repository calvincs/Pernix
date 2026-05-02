# mkcert Setup for LAN SSL

Trusted SSL certificates for local LAN access. Enables service worker registration
on mobile devices, which is required for PWA push notifications.

## On your server (Linux)

```bash
# 1. Install mkcert
sudo apt install libnss3-tools
curl -JLO "https://dl.filippo.io/mkcert/latest?for=linux/amd64"
chmod +x mkcert-v*-linux-amd64
sudo mv mkcert-v*-linux-amd64 /usr/local/bin/mkcert

# 2. Create local CA
mkcert -install

# 3. Generate cert for your LAN IP (replace with your actual IP)
mkcert -cert-file data/cert.pem -key-file data/key.pem localhost 127.0.0.1 192.168.x.x

# 4. Point Pernix at the certs (in data/settings.json or via Settings UI)
#    ssl_mode     -> "custom"
#    ssl_cert_path -> /absolute/path/to/data/cert.pem
#    ssl_key_path  -> /absolute/path/to/data/key.pem
```

## On your Android phone (one-time)

```bash
# 1. Copy the CA cert to your phone
#    The CA is at: $(mkcert -CAROOT)/rootCA.pem
#    Transfer it via USB, email, or serve it over HTTP

# 2. On Android: Settings -> Security -> Encryption & Credentials
#    -> Install a certificate -> CA Certificate -> select rootCA.pem
```

## On your iOS device (one-time)

iOS requires the CA cert wrapped in a `.mobileconfig` profile. Generate it and
serve it over HTTP so Safari can trigger the profile install prompt.

### Step 1: Generate the .mobileconfig profile

```bash
python3 -c "
import base64, uuid

with open('\$(mkcert -CAROOT)/rootCA.pem', 'rb') as f:
    pem = f.read()

lines = pem.decode().strip().split('\n')
b64 = ''.join(l for l in lines if not l.startswith('-----'))

profile = '''<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
    <key>PayloadContent</key>
    <array>
        <dict>
            <key>PayloadCertificateFileName</key>
            <string>mkcert-rootCA.crt</string>
            <key>PayloadContent</key>
            <data>''' + b64 + '''</data>
            <key>PayloadDescription</key>
            <string>mkcert local CA for LAN development</string>
            <key>PayloadDisplayName</key>
            <string>mkcert Local CA</string>
            <key>PayloadIdentifier</key>
            <string>com.mkcert.localca.cert</string>
            <key>PayloadType</key>
            <string>com.apple.security.root</string>
            <key>PayloadUUID</key>
            <string>''' + str(uuid.uuid4()).upper() + '''</string>
            <key>PayloadVersion</key>
            <integer>1</integer>
        </dict>
    </array>
    <key>PayloadDisplayName</key>
    <string>mkcert Local CA</string>
    <key>PayloadDescription</key>
    <string>Installs mkcert root CA for trusted LAN SSL</string>
    <key>PayloadIdentifier</key>
    <string>com.mkcert.localca</string>
    <key>PayloadRemovalDisallowed</key>
    <false/>
    <key>PayloadType</key>
    <string>Configuration</string>
    <key>PayloadUUID</key>
    <string>''' + str(uuid.uuid4()).upper() + '''</string>
    <key>PayloadVersion</key>
    <integer>1</integer>
</dict>
</plist>'''

with open('/tmp/mkcert-ca.mobileconfig', 'w') as f:
    f.write(profile)

print('Created /tmp/mkcert-ca.mobileconfig')
"
```

### Step 2: Serve it over HTTP from your server

```bash
cd /tmp && python3 -c "
from http.server import HTTPServer, SimpleHTTPRequestHandler
class H(SimpleHTTPRequestHandler):
    def guess_type(self, path):
        if path.endswith('.mobileconfig'):
            return 'application/x-apple-aspen-config'
        return super().guess_type(path)
HTTPServer(('0.0.0.0', 9999), H).serve_forever()
"
```

### Step 3: Open in Safari on your iOS device

Navigate to `http://<your-server-ip>:9999/mkcert-ca.mobileconfig`

Safari will prompt: "This website is trying to download a configuration profile."
Tap **Allow**.

### Step 4: Install the profile

Settings -> General -> VPN & Device Management -> tap the mkcert profile -> **Install**

### Step 5: Enable full trust (required)

Settings -> General -> About -> **Certificate Trust Settings** -> toggle ON for mkcert Local CA

Kill the temp server when done (Ctrl+C or `kill $(lsof -ti:9999)`).

## What this gives you

- Valid SSL trusted by all devices where the CA is installed
- Service worker registers on mobile -> PWA push notifications work
- No external service or domain needed -- pure LAN

## References

- [mkcert GitHub](https://github.com/FiloSottile/mkcert)
- [mkcert tutorial](https://www.tecmint.com/mkcert-create-ssl-certs-for-local-development/)
- [Android self-signed certs](https://dev.to/shaharke/using-self-signed-certificates-when-developing-android-applications-2i9e)
- [Android root cert installation](https://emteria.com/blog/install-root-certificate-android)
- [Apple - Trust manually installed certificates](https://support.apple.com/en-us/102390)
