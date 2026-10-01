#!/data/data/com.termux/files/usr/bin/bash
# Termux me JSSC scraper chalane ka script
# Setup (ek baar):
#   pkg install python git openssl-tool cronie termux-services
#   pip install requests beautifulsoup4 python-dateutil
#   git clone <repo> ~/jssc-mining-scraper   (token ke saath, taaki push ho sake)
set -e
cd ~/jssc-mining-scraper

# GlobalSign intermediate wala CA bundle (server intermediate nahi bhejta)
if [ ! -f combined-ca-bundle.pem ]; then
  CERTIFI=$(python -c "import certifi; print(certifi.where())")
  curl -sSL -o gs.crt https://secure.globalsign.com/cacert/gsrsaovsslca2018.crt
  if ! grep -q "BEGIN CERTIFICATE" gs.crt; then
    openssl x509 -inform DER -in gs.crt -out gs.pem -outform PEM
  else
    mv gs.crt gs.pem
  fi
  cat "$CERTIFI" gs.pem > combined-ca-bundle.pem
fi
export REQUESTS_CA_BUNDLE="$PWD/combined-ca-bundle.pem"

git pull --rebase --autostash
python scraper.py

git add news.json pdf/ logs/
if git diff --cached --quiet; then
  echo "Nothing new."
  exit 0
fi
git commit -m "chore: add new JSSC mining notices"
git push
