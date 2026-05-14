import urllib.parse
import json

j = {
  "xmux": {
    "cMaxReuseTimes": 0,
    "maxConcurrency": "2-4",
    "hKeepAlivePeriod": 30,
    "hMaxRequestTimes": "800-1200",
    "hMaxReusableSecs": "600-1000"
  },
  "noGRPCHeader": False,
  "xPaddingBytes": "100-1000",
  "XPaddingHeader": "X-Padding",
  "XPaddingMethod": "repeat-x",
  "uplinkHTTPMethod": "DELETE",
  "xPaddingObfsMode": True,
  "XPaddingPlacement": "header",
  "scMaxEachPostBytes": 1000000,
  "scMaxConcurrentPosts": 100,
  "scMinPostsIntervalMs": 30
}

compact_json = json.dumps(j, separators=(',', ':'))
print("extra=" + urllib.parse.quote(compact_json))
