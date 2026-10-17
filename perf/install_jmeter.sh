#!/usr/bin/env bash
# Installs Apache JMeter into perf/ and links it onto the PATH (needs Java 8+).
set -euo pipefail
VERSION="${JMETER_VERSION:-5.6.3}"
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -d "$DIR/apache-jmeter-$VERSION" ]; then
  curl -fsSL "https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-$VERSION.tgz" | tar -xz -C "$DIR"
fi
sudo ln -sf "$DIR/apache-jmeter-$VERSION/bin/jmeter" /usr/local/bin/jmeter 2>/dev/null \
  || echo "Add $DIR/apache-jmeter-$VERSION/bin to your PATH"
jmeter --version | head -n 8
