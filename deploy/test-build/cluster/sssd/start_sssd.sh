#!/bin/sh

set -e

/usr/bin/rm /var/run/sssd.pid &> /dev/null || true
chown root:root /etc/sssd/sssd.conf
/usr/sbin/sssd -i -c /etc/sssd/sssd.conf --logger=files
