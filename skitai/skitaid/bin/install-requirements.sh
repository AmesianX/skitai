#!/bin/sh -xe

# Sets up m2crypto on ubuntu architecture in virtualenv
# openssl 1.0 does not have sslv2, which is not disabled in m2crypto
# therefore this workaround is required

apt-get install swig
pip install jinja2
pip install jsonrpclib
apt-get install libpq-dev python-dev
pip install psycopg2

if [ ! -e /usr/include/openssl/opensslconf.h ]
then
	ln -s /usr/include/x86_64-linux-gnu/openssl/opensslconf.h /usr/include/openssl/
fi

PATCH="
--- SWIG/_ssl.i 2011-01-15 20:10:06.000000000 +0100
+++ SWIG/_ssl.i 2012-06-17 17:39:05.292769292 +0200
@@ -48,8 +48,10 @@
 %rename(ssl_get_alert_desc_v) SSL_alert_desc_string_long;
 extern const char *SSL_alert_desc_string_long(int);

+#ifndef OPENSSL_NO_SSL2
 %rename(sslv2_method) SSLv2_method;
 extern SSL_METHOD *SSLv2_method(void);
+#endif
 %rename(sslv3_method) SSLv3_method;
 extern SSL_METHOD *SSLv3_method(void);
 %rename(sslv23_method) SSLv23_method;"

pip install --download="." m2crypto==0.21.1
tar -xf M2Crypto-*.tar.gz
rm M2Crypto-0.21.1.tar.gz
cd M2Crypto-0.21.1
echo "$PATCH" | patch -p0
python setup.py install
cd ..
rm -rf M2Crypto-0.21.1

