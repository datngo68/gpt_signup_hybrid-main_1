#!/bin/sh
# Đo perf của upi-qr-bot trên router: chạy stripe-probe N lần song song,
# chụp RSS + CPU peak + tổng thời gian (ms timestamps via /proc/uptime).
set -e

ROUTER=192.168.8.1
N=8

ssh -o BatchMode=yes root@$ROUTER "
T_START=\$(awk '{print int(\$1*1000)}' /proc/uptime)
echo '=== before run ==='
free -m | awk '/Mem:/{print \"  used=\"\$3\"MB free=\"\$4\"MB available=\"\$7\"MB\"}'
echo '=== launching $N parallel stripe-probe ==='
PIDS=''
for i in \$(seq 1 $N); do
    /tmp/upi-qr-bot stripe-probe --bundles-cache-dir /tmp/perf-cache-\$i > /tmp/perf-\$i.log 2>&1 &
    PIDS=\"\$PIDS \$!\"
done
sleep 1
echo '--- mid-run snapshot ---'
ps aux 2>/dev/null | grep '\\[upi-qr-bot stripe' | grep -v grep | awk '{print \"  pid=\"\$2\" rss=\"\$5\"KB cpu=\"\$3\"%\"}'
TOTAL_RSS=\$(ps aux 2>/dev/null | grep upi-qr-bot | grep stripe-probe | grep -v grep | awk '{s+=\$5} END {print s}')
echo \"  total_rss=\${TOTAL_RSS}KB load=\$(awk '{print \$1}' /proc/loadavg)\"
free -m | awk '/Mem:/{print \"  used=\"\$3\"MB free=\"\$4\"MB available=\"\$7\"MB\"}'
wait \$PIDS
T_END=\$(awk '{print int(\$1*1000)}' /proc/uptime)
echo \"=== all $N done in \$((T_END - T_START))ms ===\"
PASS=0; FAIL=0
for i in \$(seq 1 $N); do
    if grep -q 'extract_config_live OK' /tmp/perf-\$i.log; then
        PASS=\$((PASS+1))
    else
        FAIL=\$((FAIL+1))
    fi
done
echo \"  pass=\$PASS fail=\$FAIL\"
SAMPLE=\$(grep -h 'extract_config_live OK' /tmp/perf-*.log | head -3)
echo \"  sample: \$SAMPLE\"
free -m | awk '/Mem:/{print \"  AFTER  used=\"\$3\"MB free=\"\$4\"MB available=\"\$7\"MB\"}'
rm -rf /tmp/perf-cache-* /tmp/perf-*.log
"
