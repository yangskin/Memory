# TLS Certificate Files

Place the issued files named for your public host in this directory on the
deployment host:

```text
memory_hub/certs/<public-hostname>.crt
memory_hub/certs/<public-hostname>.key
```

Then run `../bootstrap.sh <public-hostname>`. It downloads the public
intermediate certificate when no `root_bundle.crt` is supplied and generates
the Caddy inputs below:

```text
memory_hub/certs/fullchain.pem
memory_hub/certs/privkey.pem
```

`fullchain.pem` must contain the server certificate followed by every required
intermediate certificate. `privkey.pem` must contain the unencrypted private
key for your public host. Do not commit either file.

If your provider supplied separate files, combine the leaf certificate and
intermediate chain in this order:

```bash
cat certificate.pem intermediate-chain.pem > fullchain.pem
```

Then copy `fullchain.pem` and `privkey.pem` to this directory and restrict
their permissions:

```bash
chmod 600 fullchain.pem privkey.pem
```