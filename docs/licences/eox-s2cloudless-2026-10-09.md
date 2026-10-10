# EOxCloudless 2016: the licence we rely on, as read on 9 October 2026
The satellite imagery packs are built from EOX's `s2cloudless_3857` layer (the
2016 mosaic) by `tools/build_satellite.py`. Below are short verbatim quotes of
the two places EOX state that layer's licence, each with its URL and fetch
date; the pages themselves are EOX's. Neither host publishes a robots.txt
(on 9 October 2026, `https://cloudless.eox.at/robots.txt` returned 404 and
`https://tiles.maps.eox.at/robots.txt` returned 400).

## 1. The layer's abstract in the WMTS capabilities
Source: https://tiles.maps.eox.at/wmts/1.0.0/WMTSCapabilities.xml
Fetched: 9 October 2026, 14:28 UTC. The `<Layer>` whose `<ows:Identifier>` is
`s2cloudless_3857`, titled "Sentinel-2 cloudless layer for 2016 by EOX - 3857",
has this abstract (XML escaping as served):
> EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2016) released under &lt;a rel="license" href="https://creativecommons.org/licenses/by/4.0/"&gt;Creative Commons Attribution 4.0 International License&lt;/a&gt;.

## 2. The licence page: the 2016 licence
Source: https://cloudless.eox.at/license-non-commercial
Fetched: 9 October 2026, 14:31:03 UTC (the response's `Date` header). The
licence sentence, HTML markup removed:
> For the year 2016, EOxCloudless is licensed under the Creative Commons Attribution 4.0 International License.

## 3. The licence page: the attribution required for 2016
Same source and fetch. From the page's "Required Attribution" table, HTML
markup removed:
> "EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2016 & 2017)"

## 4. The licence page: the years after it
Same source and fetch:
> For the years 2018 to 2025, EOxCloudless WM(T)S layers is licensed under the Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International License.

## 5. How it is used
`ATTRIBUTION` in `tools/build_satellite.py` is the 2016 wording from section 3,
then the licence with its link, then a note that we modified the imagery.
`tools/test_imagery_licence.py` checks this file against it:
> EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2016 & 2017), CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). Resampled and sharpened by Trail Blazer.
