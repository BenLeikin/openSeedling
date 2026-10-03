#!/bin/sh
# Runs inside cage (see deploy/growlight-kiosk.service): turn the picture to
# portrait, wait for the dashboard to answer, then show it full screen.

if [ -n "$KIOSK_ROTATE" ] && [ "$KIOSK_ROTATE" != "0" ]; then
    out="$(wlr-randr 2>/dev/null | awk 'NR==1 {print $1}')"
    [ -n "$out" ] && wlr-randr --output "$out" --transform "$KIOSK_ROTATE" || true
fi

# After a boot or a controller restart the app takes a few seconds to come up;
# loading the page before it answers would leave an error page on screen.
i=0
while [ $i -lt 120 ]; do
    python3 -c "import urllib.request,urllib.parse,sys; urllib.request.urlopen(urllib.parse.urljoin(sys.argv[1], '/api/status'), timeout=3)" \
        "$KIOSK_URL" 2>/dev/null && break
    i=$((i + 1))
    sleep 1
done

exec cog "$KIOSK_URL"
