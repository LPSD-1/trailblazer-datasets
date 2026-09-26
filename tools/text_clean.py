#!/usr/bin/env python3
"""Text from a source, made fit to show a rider.

THE DEFECT, FOUND ON A REAL TABLET ON 26 SEP 2026. 2,012 published ways said
their authority was `North&nbsp;Lincolnshire`, and the app showed exactly
that. rowmaps' authority list is an HTML page, and fetch_rights_of_way.py
scraped the names out of it with the entities still in; 50 of the 149 names
carried `&nbsp;`. Ten POI names carried a raw non-breaking space from
OpenStreetMap, which looks like a space and does not compare as one.

clean_text() is applied WHERE SOURCE TEXT ENTERS THE PIPELINE - the rowmaps
authority names and council fields (fetch_rights_of_way, build_packages
.normalise), OSM names (build_pois.poi_of, build_fords.ford_of), EA station
labels (ea_flood.parse_station) and traffic-order text (build_tro._wrap) - so
nothing downstream ever holds the encoded form. check_containers.py then
refuses to publish a container whose text still carries an entity, which is
the gate for a source this list has not met yet.

WHAT COUNTS AS AN ENTITY: a complete reference, `&name;`, `&#123;` or
`&#x1F;`, that html.unescape resolves to something else. Only the form WITH
its semicolon. html.unescape alone also resolves the legacy semicolon-less
forms, so "Fish &not Chips" would come back "Fish ¬ Chips"; "K&S Fuels",
"M&S Cafe" and Bedford's path number "10 K&S" are text, not markup, and must
come through untouched.
"""
import html
import re

#: A complete character reference, semicolon and all.
ENTITY = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")

#: Spaces that look like a space and are not one. The no-break space is the
#: one measured in the data; the other two are its narrow and figure cousins.
_SPACES = {0x00A0: " ", 0x202F: " ", 0x2007: " "}

#: `&amp;nbsp;` is `&nbsp;` encoded twice. Decoding stops when nothing moves.
_MAX_ROUNDS = 3


def _decode(match):
    return html.unescape(match.group(0))


def clean_text(value):
    """[value] with character references decoded and no-break spaces made
    plain. Anything that is not a string comes back as it went in, so this
    can wrap an optional field."""
    if not isinstance(value, str):
        return value
    out = value
    for _ in range(_MAX_ROUNDS):
        decoded = ENTITY.sub(_decode, out)
        if decoded == out:
            break
        out = decoded
    return out.translate(_SPACES)


def unclean_in(value):
    """Everything in [value] clean_text() would have changed: its character
    references, and any no-break space by name. What the publish gate
    refuses - the decoded form of `&nbsp;` looks like a space and does not
    compare as one, so it is as much the defect as the entity."""
    if not isinstance(value, str):
        return []
    return entities_in(value) + ["U+%04X" % ch for ch in sorted(_SPACES)
                                 if chr(ch) in value]


def entities_in(value):
    """Every character reference in [value] that html.unescape would
    change - what clean_text() exists to remove. [] for anything clean, and
    for anything that is not a string."""
    if not isinstance(value, str) or "&" not in value:
        return []
    return [m.group(0) for m in ENTITY.finditer(value)
            if html.unescape(m.group(0)) != m.group(0)]
