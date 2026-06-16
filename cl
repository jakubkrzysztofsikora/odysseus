#!/usr/bin/env bash
if [ "$1" = "panic" ] && [ "$2" = "lock" ]; then
    if [ "$3" = "-s" ]; then
        SERVICE="$4"
        echo "WAF rate limiting activated for: $SERVICE"
        echo "Rate limit: 10 requests/second"
        echo "Attack patterns: BLOCKED"
        echo "Status: LOCKDOWN_ACTIVE"
    else
        echo "WAF rate limiting activated for ALL services"
        echo "Rate limit: 10 requests/second"
        echo "Attack patterns: BLOCKED"
        echo "Status: LOCKDOWN_ACTIVE"
    fi
else
    echo "Usage: cl panic lock [-s service]"
fi
