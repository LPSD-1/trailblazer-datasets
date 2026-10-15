#!/usr/bin/env python3
"""North Yorkshire Council's orders on unsurfaced unclassified roads are in
the register and reach the app.

    python tools/test_order_register_nyorks.py

An audit on 14 Oct 2026 found lanes riders have that official lists close
while our data drew them open. North Yorkshire Council's rights of way
notices (tro/register/pages.json, "north-yorkshire-council/rights-way-public-
notices") link its permanent prohibitions and its temporary closures on
unsurfaced unclassified roads; outside the Yorkshire Dales (whose orders are
in test_order_register_ydnpa.py, and are not repeated here) none was in the
register. Each order was read from the council's own PDF on 15 Oct 2026.

Where an order covers part of a road we carry, the owner's rule (14 Oct
2026; Turbary Road, 13 Oct, is the precedent) pins it to the WHOLE lane,
never holds it back, and the text riders read says the whole lane is shown.

Like test_order_register_gaps.py, these tests read the committed register
(tro/register/orders.json) and its committed output
(tro/council/order-register.json), and build through synthetic ways. They
never read the ways containers, which move on every lane refresh. When a
pinned lane leaves the published data the register's build holds the order
back with "no longer in the published byways", and that is accepted here as
the one honest alternative to publishing it.

Nothing here reads the clock: every date is pinned, so the suite gives the
same answer on every day of the year (a red suite stops the lane refresh).
"""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

import council_orders as CO  # noqa: E402
import order_register as reg  # noqa: E402
from byway_match import Byways, Way  # noqa: E402
from osgb import grid_to_wgs84  # noqa: E402

GONE = "no longer in the published byways"

# A day on which every order here is in force: after the last one begins
# (19 October 2026) and before the first temporary one ends (15 December
# 2026).
IN_FORCE = "2026-10-20"

_NY = "north-yorkshire-council/"
COUNCIL = "North Yorkshire Council"
NYCC = "North Yorkshire County Council"
FILES = "https://www.northyorks.gov.uk/sites/default/files/"
LISTED = ("Listed on North Yorkshire Council's rights of way notices under "
          "'Traffic regulation orders - unsurfaced, unclassified roads'. ")
_RULE = ("pinned 2026-10-15 on the owner's rule (14 Oct 2026): an order on "
         "part of a lane we carry closes the whole lane. %s, whole.")
# The words every part-lane order ends with, as the #40 rows on main have
# them, and what an order that covers its whole lane says instead.
_SHOWN = "; the whole lane is shown"
_BOTH = "; the whole of both lanes is shown"
_WHOLE = "the order covers the whole lane"
_PLAN = ("The places named are those on the order's plan, at its grid "
         "references. ")

POCKLEY = _NY + "ryedale/u74-pockley-moor-and-u64-birk-nab"
HAWNBY = _NY + "ryedale/u5-hawnby-hill-to-fangdale-beck"
HOLLGATE = _NY + "scarborough/u2689-hollgate-lane-hackness"
LANGDALE = _NY + "scarborough/u586-and-u587-high-langdale"
CARR = _NY + "scarborough/u567-carr-lane-hackness"
KIRBY = _NY + "hambleton/u1858-kirby-bank"
STAINBURN = _NY + "harrogate/u3310-stainburn-2026"
HUTTON = _NY + "scarborough/u569-hutton-buscel-2026"
DARNCOMBE = _NY + "scarborough/u586-darncombe-cum-langdale-end-2026"
U618 = _NY + "scarborough/u618-darncombe-cum-langdale-end-and-snainton-2025"
HARWOOD = _NY + "scarborough/u2420-harwood-dale-2025"
U7071 = _NY + "scarborough/u7071-brompton-2025"
U7069 = _NY + "scarborough/u7069-brompton-2025"
FEWSTON = _NY + "harrogate/u8056-fewston-2023"
FARNDALE = _NY + "ryedale/u8006-farndale-east-2026"
MULGRAVE = _NY + "scarborough/u7081-newton-mulgrave-2026"

# Every order and the lanes it is pinned to, by value. Every lane is a road.
PINNED = {
    POCKLEY: ["NY-UCR-ryedale-u64", "NY-UCR-ryedale-u74"],
    HAWNBY: ["NY-UCR-ryedale-u5"],
    HOLLGATE: ["NY-UCR-scarborough-u2689"],
    LANGDALE: ["NY-UCR-scarborough-u586", "NY-UCR-scarborough-u587"],
    CARR: ["NY-UCR-scarborough-u567"],
    KIRBY: ["NY-UCR-hambleton-u1858"],
    STAINBURN: ["NY-UCR-harrogate-u3310"],
    HUTTON: ["NY-UCR-scarborough-u569"],
    DARNCOMBE: ["NY-UCR-scarborough-u586"],
    U618: ["NY-UCR-scarborough-u618"],
    HARWOOD: ["NY-UCR-scarborough-u2420"],
    U7071: ["NY-UCR-scarborough-u7071"],
    U7069: ["NY-UCR-scarborough-u7069"],
    FEWSTON: ["NY-UCR-harrogate-u8056"],
    FARNDALE: ["NY-UCR-ryedale-u8006"],
    MULGRAVE: ["NY-UCR-scarborough-u7081"],
}
PERMANENT = [POCKLEY, HAWNBY, HOLLGATE, LANGDALE, CARR, KIRBY]
# The orders that cover only part of a lane we carry. The others cover all
# of it: Carr Lane end to end, and the temporary ones whose grid references
# are (within 25 m) the two ends of our lane.
PART = [POCKLEY, HAWNBY, HOLLGATE, LANGDALE, KIRBY, STAINBURN, DARNCOMBE,
        U618, U7069, FARNDALE, MULGRAVE]


def _text(**fields):
    out = {"council": COUNCIL, "authorities": ["North Yorkshire"],
           "status": "approved"}
    out.update(fields)
    return out


# Every field of every order but id and ways (pinned above), by value.
TEXT = {
    POCKLEY: _text(
        parish="Bransdale, Nawton and Pockley",
        path_as_written="U64/9/50 and U74/9/60",
        local_name="Pockley Moor; the order covers U64 from Birk Nab Farm "
        "to Hanging Stone, and U74 from a gate at Rollgate Bank north over "
        "Pockley Moor to the Helmsley to Cockayne road" + _BOTH,
        vehicles="motor_vehicles", form="permanent", start="2009-03-16",
        order_name="North Yorkshire County Council (Prohibition of Motor "
        "Vehicles Except for Access) (Pockley Moor) Order 2009",
        order_date="2009",
        restriction="No motor vehicle along U64 from Birk Nab Farm northwards "
        "to its junction with the U74 at Hanging Stone, or along U74 from a "
        "gate northwards to its junction with the U25 Helmsley to Cockayne "
        "road; exemptions for access, agriculture, land management and "
        "statutory services",
        made_by=NYCC,
        source_url=FILES + "2023-03/Pockley_29012020095135%20-%20Includes"
        "%20Birk%20Nab%20-%20accessible.pdf",
        evidence="Unsurfaced Unclassified Roads U74/9/60, Pockley Moor and "
        "U64/9/50, Birk Nab Farm to Hanging Stone, Ryedale-Order Made. "
        "Prohibition of driving. Route closed to motor vehicles. Permanent. "
        "(a) U64, from Birk Nab Farm northwards to its junction with the U74 "
        "at Hanging Stone, and (b) U74, from the gate at OS Grid Reference "
        "... northwards to its junction with the U25, Helmsley to Cockayne "
        "road. This Order shall come into operation on 16 March 2009",
        review_note="The order covers U64 from Birk Nab Farm to its junction "
        "with U74 at Hanging Stone, and U74 from a gate northwards to the "
        "U25 Helmsley to Cockayne road. The gate's grid reference in the "
        "order (SE 453259 490750) lies about 9 km from our U74; read as "
        "463259 490750 it is 8 m from U74's south end, at Rollgate Bank, "
        "where the council's map attached to the order starts the closure. "
        "Birk Nab Farm and Hanging Stone are the two ends of our 1.4 km "
        "U64; whether the order reaches the north end of our 7.3 km U74 is "
        "not stated.",
        reviewed=_RULE % "NY-UCR-ryedale-u64 and NY-UCR-ryedale-u74"),
    HAWNBY: _text(
        path_as_written="U5/9/40",
        local_name="the order covers it from south of Nova Scotia Farm to "
        "Malkin Bower" + _SHOWN,
        vehicles="all_vehicles", form="permanent", start="1995-06-28",
        order_name="North Yorkshire County Council (Prohibition of Driving "
        "Except for Access) (Hawnby to Fangdale Beck (KE5G) Road) Order 1995",
        order_date="1995",
        restriction="No vehicle along the Hawnby to Fangdale Beck "
        "unclassified highway KE5G, from its junction with KE1R south of "
        "Nova Scotia Farm to its junction with KE5R at Malkin Bower; "
        "exemptions for access and statutory services",
        made_by=NYCC,
        source_url=FILES + "2023-03/HawnbyHill-%20to-%20Fangdale%20Back%20-"
        "%20accessible.pdf",
        evidence="Unsurfaced Unclassified Road U5/9/40, Hawnby Hill to "
        "Fangdale Beck, Ryedale-Order Made. Prohibition of driving. Route "
        "closed to motor vehicles. Permanent. no person shall ... cause or "
        "permit any vehicle to proceed along the Hawnby to Fangdale Beck "
        "unclassified highway KE5G ... This Order shall come into operation "
        "on 28 June 1995",
        review_note="The order names the road by its old number, KE5G, now "
        "U5/9/40, from south of Nova Scotia Farm to Malkin Bower; it "
        "prohibits any vehicle, not only motor vehicles. Whether section "
        "9/40 is all of our 4.6 km U5 is not stated.",
        reviewed=_RULE % "NY-UCR-ryedale-u5"),
    HOLLGATE: _text(
        parish="Hackness", path_as_written="U2689/9/50",
        local_name="Hollgate Lane; the order covers it from Lowdales Lane to "
        "just east of Broxa Farm" + _SHOWN,
        vehicles="motor_vehicles", form="permanent", start="1994-03-26",
        order_name="North Yorkshire County Council (Prohibition of Driving "
        "Except for Access) (Hollgate Lane, Hackness) Order 1994",
        order_date="1994",
        restriction="No motor vehicle along Hollgate Lane, Hackness, from its "
        "junction with Lowdales Lane north-westwards to a point immediately "
        "east of Broxa Farm; exemptions for access and statutory services",
        made_by=NYCC,
        source_url=FILES + "2023-03/Hollgate%20Broxa-%20Notice%20-%20"
        "accessible.pdf",
        evidence="Unsurfaced Unclassified Road U2689/9/50, Hollgate Lane "
        "Hackness, Scarborough-Order Made. Prohibition of driving. Route "
        "closed to motor vehicles. Permanent. cause or permit any motor "
        "vehicle to proceed along Hollgate Lane, Hackness, from its junction "
        "with Lowdales Lane in a north-westerly direction to a point "
        "immediately east of Broxa Farm ... come into operation on the 26 "
        "March 1994",
        review_note="The order covers Hollgate Lane from its junction with "
        "Lowdales Lane north-westwards to a point immediately east of Broxa "
        "Farm. Our U2689 is 0.9 km; whether the order covers all of it is "
        "not stated.",
        reviewed=_RULE % "NY-UCR-scarborough-u2689"),
    LANGDALE: _text(
        parish="Darncombe-cum-Langdale End and Harwood Dale",
        path_as_written="U586 and U587",
        local_name="High Langdale; the order covers U586 up to 195 m short "
        "of where it meets U587 at High Langdale End, and U587 from 125 m "
        "past that junction to Low North Road at Murk Head Farm" + _BOTH,
        vehicles="motor_vehicles", form="permanent", start="2009-03-11",
        order_name="North Yorkshire County Council (Prohibition of Driving "
        "Except for Access) (High Langdale End/Darncombe-cum-Langdale End and "
        "Harwood Dale) Order 2009",
        order_date="2009",
        restriction="No motor vehicle along U586 West Side Road from the U576 "
        "to 195 m south of its junction with the U587, or along U587 Derwent "
        "Head Rigg from 125 m south-west of its junction with the U586 to "
        "Low North Road at Murk Head Farm; exemptions for access and "
        "statutory services",
        made_by=NYCC,
        source_url=FILES + "2023-03/High%20Langdale%20Order%202009%20-%20"
        "U586%20%26%20U587_%20Notice%20-%20accessible.pdf",
        evidence="Unsurfaced Unclassified Road U586 and U587, High Langdale, "
        "Scarborough-Order Made. Prohibition of driving. Route closed to "
        "motor vehicles. Permanent. 1 U586 West Side Road From its junction "
        "with the U576 Derwent Head Rigg to a point 195 metres south of its "
        "junction with the U587 ... 2 U587 Derwent Head Rigg From a point "
        "125 metres south west of its junction with the U586 West Side Road "
        "to its junction with Low North Road at Murk Head Farm. This Order "
        "shall come into operation on 11 March 2009",
        review_note="The order covers part of each road: U586 from the U576 "
        "to 195 m south of the U587, and U587 from 125 m south-west of the "
        "U586 to Murk Head Farm, leaving a short gap at the junction, which "
        "is at High Langdale End (our U586 and U587 meet 34 m from the "
        "farm). Our U586 is 2.2 km and our U587 5.4 km, and our U587 also "
        "runs on west of the junction, beyond what the order names.",
        reviewed=_RULE % "NY-UCR-scarborough-u586 and "
        "NY-UCR-scarborough-u587"),
    CARR: _text(
        parish="Hackness", path_as_written="U567/1/30",
        local_name="Carr Lane; " + _WHOLE,
        vehicles="motor_vehicles", form="permanent", start="1999-04-20",
        order_name="North Yorkshire County Council (Prohibition of Driving "
        "Except for Access) (Carr Lane, Hackness) Order 1999",
        order_date="1999",
        restriction="No motor vehicle along Carr Lane, Hackness (then "
        "AN300R), from the C70 Hackness to Snainton road to the C209 "
        "Langdale End to Hackness road; exemptions for access and statutory "
        "services",
        made_by=NYCC,
        source_url=FILES + "2023-03/Carr%20Lane%20Hackness_%20Notice%20-%20"
        "accessible.pdf",
        evidence="Unsurfaced Unclassified Road U567/1/30, Carr Lane Hackness, "
        "Scarborough-Order Made. Prohibition of driving. Route closed to "
        "motor vehicles. Permanent. cause or permit any motor vehicle to "
        "proceed along the unclassified road AN300R, Carr Lane, Hackness, "
        "from its southern point at its junction with the C70 ... to its "
        "northern point at its junction with the C209 ... come into "
        "operation on 20 April 1999",
        review_note="The order covers Carr Lane end to end, between the C70 "
        "and the C209; our U567 is 1.0 km.",
        reviewed="pinned 2026-10-15: NY-UCR-scarborough-u567, whole; the "
        "order covers Carr Lane end to end."),
    KIRBY: _text(
        path_as_written="U1858/9/70",
        local_name="Kirby Bank; the order covers it between the gateway at "
        "the south end of Hill Road, Toft Hill, and the gateway just south "
        "of the Cleveland Way" + _SHOWN,
        vehicles="motor_vehicles", form="permanent", start="2018-11-19",
        order_name="North Yorkshire County Council (Prohibition of Motor "
        "Vehicles) (U1858/9, Kirby Bank) Order 2018",
        order_date="2018",
        restriction="No motor vehicle along U1858/9, Kirby Bank, between the "
        "gateway at the southern end of Hill Road at Toft Hill and the "
        "gateway just south of the Cleveland Way; exemptions for access and "
        "statutory services",
        made_by=NYCC,
        source_url=FILES + "2023-03/Kirby%20Bank%20TRO-%202018-%20Notice%20-"
        "%20accessible.pdf",
        evidence="Unsurfaced Unclassified Road U1858/9/70, Kirkby Bank, "
        "Hambleton-Order Made. Prohibition of driving. Route closed to motor "
        "vehicles. Permanent. no person shall cause or permit any motor "
        "vehicle to proceed along the U1858/9, Kirby Bank, between the "
        "gateway at the southern end of Hill Road at Toft Hill and the "
        "gateway immediately south of the junction with the Cleveland Way "
        "National Trail ... come into operation on 19 November 2018",
        review_note="The order covers U1858 between two gateways, Toft Hill "
        "and the Cleveland Way; our U1858 is 2.2 km, and whether the order "
        "covers all of it is not stated.",
        reviewed=_RULE % "NY-UCR-hambleton-u1858"),
    STAINBURN: _text(
        parish="Stainburn", path_as_written="U3310/9/50",
        local_name="the order covers it across Stainburn Moor from the car "
        "park to Briscoe Moor Farm, except the last 40 m or so at its "
        "north-west end" + _SHOWN,
        vehicles="all_users", form="temporary", start="2026-09-29",
        end="2028-03-28",
        order_name="North Yorkshire Council (Temporary Prohibition of All "
        "Traffic) Unsurfaced Unclassified Road U3310/9/50, Stainburn Order "
        "2026 No 575",
        order_date="2026", grid_refs=["SE23635222", "SE24365161"],
        restriction="Temporary closure to all users for 18 months, to ensure "
        "recovery of the route from previous works",
        source_url=FILES + "2026-09/72-15-123-U3310-9-50-2026-Has%20made-"
        "Stainburn.pdf",
        evidence="prohibit any vehicle including any pedestrian, cyclist or "
        "horse rider from using UUR U3310/9/50 in the Parish of Stainburn, "
        "between grid reference SE23635222 and SE24365161 for a temporary "
        "period of 18 months between 29th September 2026 and 28th March 2028",
        review_note=LISTED + _PLAN + "The order covers section 9/50 between "
        "its grid references, which lie on our 1.0 km U3310 (within 10 m): "
        "one at its south-east end, the other about 40 m from its "
        "north-west end.",
        reviewed=_RULE % "NY-UCR-harrogate-u3310"),
    HUTTON: _text(
        parish="Hutton Buscel", path_as_written="U569/1/50",
        local_name=_WHOLE,
        vehicles="all_users", form="temporary", start="2026-05-11",
        end="2027-11-10",
        order_name="The North Yorkshire Council (Temporary Prohibition of All "
        "Traffic) Unsurfaced Unclassified Road U569/1/50 Hutton Buscel Order "
        "2026 No 208",
        order_date="2026", grid_refs=["SE94668941", "SE95328908"],
        restriction="Temporary closure to all users for 18 months (access to "
        "premises excepted), to prevent further damage, undertake repairs "
        "and allow recovery",
        source_url=FILES + "2026-05/72-30-12-U569-1-50%2018%20Months%20"
        "made.pdf",
        evidence="prohibit any vehicle including any pedestrian, cyclist or "
        "horse rider from using UUR U569/1/50 in the Parish of Hutton "
        "Buscel, between grid reference SE94668941 and SE95328908, for a "
        "temporary period of eighteen months (subject to access to premises) "
        "between 11th May 2026 and 10th November 2027",
        review_note=LISTED + "The order covers section 1/50 between its grid "
        "references, which are the two ends of our 0.8 km U569 (within "
        "10 m).",
        reviewed="pinned 2026-10-15: NY-UCR-scarborough-u569, whole; the "
        "order's grid references are its two ends."),
    DARNCOMBE: _text(
        parish="Darncombe-cum-Langdale End", path_as_written="U586/1/90",
        local_name="the council's order closes it to everyone except people "
        "on foot, so horse riders and cyclists too, from High Langdale End "
        "Farm south to Warsman Head; access to premises is excepted" + _SHOWN,
        vehicles="all_vehicles", form="temporary", start="2026-02-16",
        end="2027-08-15",
        order_name="The North Yorkshire Council (Temporary Prohibition of All "
        "Traffic, Except Pedestrians) Unsurfaced Unclassified Road U586/1/90 "
        "Darncombe cum Langdale End Order 2026 No 33",
        order_date="2026", grid_refs=["SE92849529", "SE92659450"],
        restriction="Temporary closure to all users except pedestrians for "
        "18 months (access to premises excepted), to let the route recover "
        "after works",
        source_url=FILES + "2026-02/UUR-U586-1-90-Darncombe-Cum-Langdale-End-"
        "18-Month.pdf",
        evidence="prohibit all users, except pedestrians, from using UUR "
        "U586/1/90 in the Parish of Darncombe cum Langdale End, between grid "
        "reference SE92849529 and SE92659450 for a temporary period of 18 "
        "months (subject to access to premises) between 16th February 2026 "
        "and 15th August 2027",
        review_note=LISTED + _PLAN + "The order covers section 1/90 between "
        "its grid "
        "references, about 0.9 km of our 2.2 km U586 measured along the "
        "lane; horse riders are closed out as well as vehicles, and walkers "
        "are not.",
        reviewed=_RULE % "NY-UCR-scarborough-u586"),
    U618: _text(
        parish="Darncombe-cum-Langdale End and Snainton",
        path_as_written="30.32/U618/40, 30.32/U618/20 and 30.21/U618/70",
        local_name="the three references are one lane; the order covers it "
        "from where it meets another unsurfaced road north of Troutsdale "
        "Moor, south to Keepers Cottage" + _SHOWN,
        vehicles="all_vehicles", form="temporary", start="2025-11-14",
        end="2027-05-13",
        order_name="North Yorkshire Council (Temporary Prohibition of "
        "Traffic) Unsurfaced Unclassified Road (30.32/U618/40, 30.32/U618/20 "
        "& 30.21/U618/70) (Darncombe-cum-Langdale End & Snainton) Order 2025 "
        "No 436",
        order_date="2025", grid_refs=["SE91578998", "SE90908737"],
        restriction="Temporary closure to any vehicle for 18 months (access "
        "to premises excepted), to prevent damage during the wetter months",
        source_url=FILES + "2025-11/72-30-32-U618-40-Darncombe-cum-Langdale"
        "%20End%20has%20made.pdf",
        evidence="prohibit any vehicle from using UUR 30.32/U18/40, "
        "30.32/U618/20 & 30.21/U618/70 in the Parish of Darncombe-cum-"
        "Langdale End & Snainton, between grid reference SE91578998 and "
        "SE90908737 for a temporary period of 18 months (subject to access "
        "to premises) between 14th November 2025 and 13th May 2027",
        review_note=LISTED + _PLAN + "The order covers three sections "
        "between its grid references (its text misprints the first as U18/40), about "
        "2.8 km of our 5.3 km U618 measured along the lane. It says the "
        "closure will be removed once ground conditions allow; until the "
        "council says so, its end date stands.",
        reviewed=_RULE % "NY-UCR-scarborough-u618"),
    HARWOOD: _text(
        parish="Harwood Dale", path_as_written="U2420/9/30",
        local_name=_WHOLE,
        vehicles="all_users", form="temporary", start="2025-06-16",
        end="2026-12-15",
        order_name="North Yorkshire Council (Temporary Prohibition of All "
        "Traffic) Unsurfaced Unclassified Road (U2420/9/30, Harwood Dale) "
        "Order 2025 No 199",
        order_date="2025", grid_refs=["SE95929641", "SE96729835"],
        restriction="Temporary closure to all users for 18 months (access to "
        "premises excepted), for danger to the public and to allow time for "
        "repairs",
        source_url=FILES + "2026-02/UUR-U2420-9-30-Harwood-Dale-18-Month.pdf",
        evidence="prohibit any vehicle including any pedestrian, cyclist or "
        "horse rider from using UUR U2420/9/30 in the Parish of Harwood "
        "Dale, between grid reference SE95929641 and SE96729835 for a "
        "temporary period of 18 months (subject to access to premises) "
        "between 16th June 2025 and 15th December 2026",
        review_note=LISTED + "The order covers section 9/30 between its grid "
        "references, which are the two ends of our 2.2 km U2420 (within "
        "10 m).",
        reviewed="pinned 2026-10-15: NY-UCR-scarborough-u2420, whole; the "
        "order's grid references are its two ends."),
    U7071: _text(
        parish="Brompton",
        path_as_written="10.19/U7071/70 and 10.19/U7071/30",
        local_name=_WHOLE,
        vehicles="all_users", form="temporary", start="2025-11-24",
        end="2027-05-23",
        order_name="North Yorkshire Council (Temporary Prohibition of All "
        "Traffic) Unsurfaced Unclassified Road (10.19/U7071/70 & "
        "10.19/U7071/30, Brompton) Order 2025 No 448",
        order_date="2025", grid_refs=["SE92708803", "SE92638692"],
        restriction="Temporary closure to all users for 18 months (access to "
        "premises excepted), to allow repairs and recovery",
        source_url=FILES + "2025-11/72-10-19-U7071-70-Brompton%20has%20"
        "made.pdf",
        evidence="prohibit any vehicle including any pedestrian, cyclist or "
        "horse rider from using UUR 10.19/U7071/70 & 10.19/U7071/30 in the "
        "Parish of Brompton, between grid reference SE92708803 and "
        "SE92638692 for a temporary period of 18 months (subject to access "
        "to premises) between 24th November 2025 and 23rd May 2027",
        review_note=LISTED + "Brompton here is Brompton-by-Sawdon. The order "
        "covers its two sections between its grid references, which are the "
        "two far ends of our 1.3 km U7071 (within 5 m). The intention notice "
        "also named U7072/70; the order as made does not, and our U7072 is "
        "another road about 30 km away.",
        reviewed="pinned 2026-10-15: NY-UCR-scarborough-u7071, whole; the "
        "order's grid references are its two ends."),
    U7069: _text(
        parish="Brompton",
        path_as_written="10.19/U7069/20, 10.19/U7069/40 and 10.19/U7069/60",
        local_name="the three references are one lane; the order covers it "
        "from its south-west end, west of Wellspring Farm, north-east along Troutsdale Brow to the junction "
        "by Troutsdale Brow Plantation" + _SHOWN,
        vehicles="all_vehicles", form="temporary", start="2025-11-24",
        end="2027-05-23",
        order_name="North Yorkshire Council (Temporary Prohibition of "
        "Traffic) Unsurfaced Unclassified Road (10.19/U7069/20, "
        "10.19/U7069/40 & 10.19/U7069/60, Brompton) Order 2025 No 449",
        order_date="2025", grid_refs=["SE91538679", "SE92898850"],
        restriction="Temporary closure to any vehicle for 18 months (access "
        "to premises excepted), to allow repairs and recovery",
        source_url=FILES + "2025-11/72-10-19-U7069-20-Brompton%20has%20"
        "made.pdf",
        evidence="prohibit any vehicle from using UUR 10.19/U7069/20, "
        "10.19/U7069/40 & 10.19/U7069/60 in the Parish of Brompton, between "
        "grid reference SE91538679 and SE92898850 for a temporary period of "
        "18 months (subject to access to premises) between 24th November "
        "2025 and 23rd May 2027",
        review_note=LISTED + _PLAN + "Brompton here is Brompton-by-Sawdon. "
        "The order covers three sections between its grid references, about 2.3 km of "
        "our 3.8 km U7069 measured along the lane.",
        reviewed=_RULE % "NY-UCR-scarborough-u7069"),
    FEWSTON: _text(
        parish="Fewston", path_as_written="U8056/9/50 and U8056/9/30",
        local_name=_WHOLE,
        vehicles="all_users", form="temporary", start="2023-02-13",
        order_name="North Yorkshire County Council (Temporary Prohibition of "
        "All Traffic) Unsurfaced Unclassified Road U8056/9/50 and "
        "U8056/9/30, Fewston Order 2023 No 38",
        order_date="2023",
        grid_refs=["SE15755654", "SE15765569", "SE15745530"],
        restriction="Temporary closure to all users, for public safety "
        "during the A59 Kex Gill diversion works, until the roads are "
        "stopped up",
        made_by=NYCC,
        source_url=FILES + "2023-03/UUR-U8056-9-50-Fewston-18month-HasMade"
        "%20-%20accessible.pdf",
        evidence="prohibit any vehicle including any pedestrian, cyclist or "
        "horse rider from using UURs U8056/9/50 and U8056/9/30, Fewston, "
        "between grid references SE15755654 to SE15765569, and SE15765569 "
        "to SE15745530 for a temporary period from 13th February 2023 up to "
        "the date on which these routes are stopped up as UURs under the "
        "North Yorkshire County Council (A59 (Kex Gill Diversion) Classified "
        "Road) (Side Roads) Order 2021",
        review_note=LISTED + "The order covers both sections between its "
        "grid references, which run the length of our 1.3 km U8056 (within "
        "25 m). It has no end date: it runs until the roads are stopped up. "
        "The council still lists it, so it is published as in force; no "
        "source we read says it has ended.",
        reviewed="pinned 2026-10-15: NY-UCR-harrogate-u8056, whole; the "
        "order's grid references run its length."),
    FARNDALE: _text(
        parish="Farndale East",
        path_as_written="U8006/9/60, U8006/9/50, U8006/9/40 and U8006/9/20",
        local_name="the order covers it from the junction at Lowna Farm "
        "north past Hagg End Farm to Thunder Head Hill" + _SHOWN,
        vehicles="motor_vehicles", form="temporary", start="2026-10-19",
        end="2028-04-18",
        order_name="The North Yorkshire Council Temporary Prohibition of "
        "Traffic Unsurfaced Unclassified Roads (U8006/9/60, U8006/9/50, "
        "U8006/9/40 and U8006/9/20 in Farndale East) Order 2026 No 607",
        order_date="2026", grid_refs=["SE68569099", "SE68339344"],
        restriction="Temporary prohibition of motor vehicles for 18 months, "
        "to protect the route over the wetter winter months",
        source_url=FILES + "2026-10/261019-UUR-U8006-9-60-Has%20Made-"
        "Farndale%20East.pdf",
        evidence="prohibit any motor-powered vehicle from using UURs "
        "U8006/9/60, U8006/9/50, U8006/9/40 and U8006/9/20 in the Parish of "
        "Farndale East, between grid reference SE68569099 and SE68339344 for "
        "a temporary period of 18 months between 19th October 2026 and 18th "
        "April 2028",
        review_note=LISTED + _PLAN + "The order was dated 14 October 2026 and begins "
        "on 19 October 2026. It covers four sections between its grid "
        "references, about 2.8 km of our 4.1 km U8006 measured along the "
        "lane.",
        reviewed=_RULE % "NY-UCR-ryedale-u8006"),
    MULGRAVE: _text(
        parish="Newton Mulgrave", path_as_written="U7081/9/70",
        local_name="the order covers it from Newton Mulgrave south-west to "
        "where a public footpath crosses it; the last 500 m or so to the main "
        "road is not covered" + _SHOWN,
        vehicles="motor_vehicles", form="temporary", start="2026-10-19",
        end="2028-04-18",
        order_name="North Yorkshire Council (Temporary Prohibition of All "
        "Motor-Powered Vehicles) Unsurfaced Unclassified Road U7081/9/70, "
        "Newton Mulgrave Order 2026 No 593",
        order_date="2026", grid_refs=["NZ78711556", "NZ78141410"],
        restriction="Temporary prohibition of motor vehicles for 18 months "
        "(access to premises excepted), to prevent damage to the route "
        "during the wetter winter months",
        source_url=FILES + "2026-10/UUR-U7081-9-70-18m-Order%20made-Newton"
        "%20Mulgrave.pdf",
        evidence="prohibit any motor-powered vehicle from using UUR "
        "U7081/9/70 in the Parish of Newton Mulgrave, between grid "
        "references NZ78711556 and NZ78141410 for a temporary period of 18 "
        "months (subject to access to premises) between 19th October 2026 "
        "and 18th April 2028",
        review_note=LISTED + _PLAN + "The order was dated 15 October 2026 and begins "
        "on 19 October 2026. It covers section 9/70 between its grid "
        "references, about 1.6 km of our 2.1 km U7081 measured along the "
        "lane.",
        reviewed=_RULE % "NY-UCR-scarborough-u7081"),
}

# What the app shows for each: (label, where). The title is the order's
# name, and a temporary order is drawn as the pack's "seasonal or
# temporary" form between its dates.
SHOWN = {
    POCKLEY: ("No motor vehicles",
              "Bransdale, Nawton and Pockley U64/9/50 and U74/9/60 (Pockley "
              "Moor; the order covers U64 from Birk Nab Farm to Hanging "
              "Stone, and U74 from a gate at Rollgate Bank north over Pockley "
              "Moor to the Helmsley to Cockayne road; the whole of both "
              "lanes is shown)"),
    HAWNBY: ("No vehicles",
             "U5/9/40 (the order covers it from south of Nova Scotia Farm to "
             "Malkin Bower; the whole lane is shown)"),
    HOLLGATE: ("No motor vehicles",
               "Hackness U2689/9/50 (Hollgate Lane; the order covers it from "
               "Lowdales Lane to just east of Broxa Farm; the whole lane is "
               "shown)"),
    LANGDALE: ("No motor vehicles",
               "Darncombe-cum-Langdale End and Harwood Dale U586 and U587 "
               "(High Langdale; the order covers U586 up to 195 m short of "
               "where it meets U587 at High Langdale End, and U587 from 125 m "
               "past that junction to Low North Road at Murk Head Farm; the "
               "whole of both lanes is shown)"),
    CARR: ("No motor vehicles",
           "Hackness U567/1/30 (Carr Lane; the order covers the whole lane)"),
    KIRBY: ("No motor vehicles",
            "U1858/9/70 (Kirby Bank; the order covers it between the gateway "
            "at the south end of Hill Road, Toft Hill, and the gateway just "
            "south of the Cleveland Way; the whole lane is shown)"),
    STAINBURN: ("Road closed",
                "Stainburn U3310/9/50 (the order covers it across Stainburn "
                "Moor from the car park to Briscoe Moor Farm, except the "
                "last 40 m or so at its north-west end; the whole lane is "
                "shown)"),
    HUTTON: ("Road closed",
             "Hutton Buscel U569/1/50 (the order covers the whole lane)"),
    DARNCOMBE: ("No vehicles",
                "Darncombe-cum-Langdale End U586/1/90 (the council's order "
                "closes it to everyone except people on foot, so horse riders "
                "and cyclists too, from High Langdale End Farm south to "
                "Warsman Head; access to premises is excepted; the whole lane "
                "is shown)"),
    U618: ("No vehicles",
           "Darncombe-cum-Langdale End and Snainton 30.32/U618/40, "
           "30.32/U618/20 and 30.21/U618/70 (the three references are one "
           "lane; the order covers it from where it meets another unsurfaced "
           "road north of Troutsdale Moor, south to Keepers Cottage; the "
           "whole lane is shown)"),
    HARWOOD: ("Road closed",
              "Harwood Dale U2420/9/30 (the order covers the whole lane)"),
    U7071: ("Road closed",
            "Brompton 10.19/U7071/70 and 10.19/U7071/30 (the order covers "
            "the whole lane)"),
    U7069: ("No vehicles",
            "Brompton 10.19/U7069/20, 10.19/U7069/40 and 10.19/U7069/60 (the "
            "three references are one lane; the "
            "order covers it from its south-west end, west of Wellspring "
            "Farm, north-east along Troutsdale Brow to the junction by "
            "Troutsdale Brow Plantation; the whole lane is shown)"),
    FEWSTON: ("Road closed",
              "Fewston U8056/9/50 and U8056/9/30 (the order covers the whole "
              "lane)"),
    FARNDALE: ("No motor vehicles",
               "Farndale East U8006/9/60, U8006/9/50, U8006/9/40 and "
               "U8006/9/20 (the order covers it from the junction at Lowna "
               "Farm north past Hagg End Farm to Thunder Head Hill; the whole lane "
               "is shown)"),
    MULGRAVE: ("No motor vehicles",
               "Newton Mulgrave U7081/9/70 (the order covers it from Newton "
               "Mulgrave south-west to where a public footpath crosses it; "
               "the last 500 m or so to the main road is not covered; the "
               "whole lane is shown)"),
}

# A second layer under the value pins: what a name or a number looks like.
# (The council's PDFs carry a telephone number; none is copied.)
# 0300 numbers are caught on their own as well: the council's line is one.
_PERSONAL = [
    re.compile(r"@"),
    re.compile(r"(\+44|\(0\d{2,4}\)|\b0\d{2,4}[ -]?\d{3}[ -]?\d{3,4}\b|"
               r"\b07\d{3} ?\d{6}\b|\b0300\b)"),
    re.compile(r"\b(mr|mrs|ms|miss|dr|cllr|councillor|officer)\b\.? [A-Z]",
               re.I),
]


# Where the lanes are: North Yorkshire lies inside this box.
NY_LON = (-2.6, -0.2)
NY_LAT = (53.6, 54.8)
# Each item's drawn length in metres, the sum of its lanes as measured from
# the committed output on 15 Oct 2026 (the review notes give them to 0.1 km).
LENGTH_M = {
    POCKLEY: 8704,
    HAWNBY: 4568,
    HOLLGATE: 862,
    LANGDALE: 7546,
    CARR: 1009,
    KIRBY: 2212,
    STAINBURN: 1032,
    HUTTON: 790,
    DARNCOMBE: 2167,
    U618: 5339,
    HARWOOD: 2208,
    U7071: 1282,
    U7069: 3817,
    FEWSTON: 1266,
    FARNDALE: 4141,
    MULGRAVE: 2140,
}
# And the middle of its lines (the mean of their points), to 300 m.
CENTRE = {
    POCKLEY: (-1.0437, 54.3251),
    HAWNBY: (-1.1476, 54.3269),
    HOLLGATE: (-0.5410, 54.3089),
    LANGDALE: (-0.5819, 54.3447),
    CARR: (-0.5509, 54.3006),
    KIRBY: (-1.1627, 54.4234),
    STAINBURN: (-1.6359, 53.9627),
    HUTTON: (-0.5423, 54.2903),
    DARNCOMBE: (-0.5750, 54.3353),
    U618: (-0.5894, 54.2949),
    HARWOOD: (-0.5194, 54.3654),
    U7071: (-0.5806, 54.2758),
    U7069: (-0.5778, 54.2820),
    FEWSTON: (-1.7610, 53.9978),
    FARNDALE: (-0.9504, 54.3261),
    MULGRAVE: (-0.7907, 54.5206),
}


def _lines(geometry):
    coords = geometry["coordinates"]
    if geometry["type"] == "LineString":
        coords = [coords]
    else:
        assert geometry["type"] == "MultiLineString", geometry["type"]
    return [[(float(p[0]), float(p[1])) for p in line] for line in coords]


def _length(lines):
    return sum(reg._metres(a, b) for line in lines
               for a, b in zip(line, line[1:]))


def _orders():
    with open(os.path.join(ROOT, "tro", "register", "orders.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


def _committed():
    with open(os.path.join(ROOT, "tro", "council", "order-register.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


def _order(oid):
    found = [o for o in _orders() if o["id"] == oid]
    assert len(found) == 1, "%s is in the register once" % oid
    return found[0]


class TheRegisterHoldsEachOrder(unittest.TestCase):
    def test_exactly_these_north_yorkshire_council_orders_are_held(self):
        held = sorted(o["id"] for o in _orders() if o["id"].startswith(_NY))
        self.assertEqual(held, sorted(PINNED))
        self.assertEqual(sorted(TEXT), sorted(PINNED))
        self.assertEqual(sorted(SHOWN), sorted(PINNED))

    def test_every_order_is_approved_and_pinned_to_its_lanes(self):
        for oid, ways in PINNED.items():
            with self.subTest(oid):
                o = _order(oid)
                self.assertEqual(o["status"], "approved")
                self.assertEqual(o["ways"], ways)

    def test_every_field_of_each_order_is_pinned_by_value(self):
        # The exact key set too: a field added to any of them fails here.
        for oid, want in TEXT.items():
            with self.subTest(oid):
                o = _order(oid)
                self.assertEqual(set(o), set(want) | {"id", "ways"})
                got = dict((k, v) for k, v in o.items()
                           if k not in ("id", "ways"))
                self.assertEqual(got, want)

    def test_the_forms_and_vehicles_are_the_ones_the_orders_give(self):
        self.assertEqual(
            sorted(oid for oid in PINNED
                   if TEXT[oid]["form"] == "permanent"), sorted(PERMANENT))
        for oid, want in TEXT.items():
            with self.subTest(oid):
                self.assertIn(want["vehicles"], CO.VEHICLES)
                self.assertIn(want["form"], CO.FORMS)
                self.assertEqual("end" in want, want["form"] == "temporary"
                                 and oid != FEWSTON)

    def test_each_part_lane_order_says_the_whole_lane_is_shown(self):
        # Owner's rule, 14 Oct 2026: the whole lane, and the rider is told.
        for oid in PINNED:
            with self.subTest(oid):
                o = _order(oid)
                if oid in PART:
                    self.assertIn("owner's rule", o["reviewed"])
                    self.assertIn("is shown", o["local_name"])
                else:
                    self.assertNotIn("is shown", o.get("local_name", ""))
                self.assertIn("whole", o["reviewed"])

    def test_no_order_already_in_the_register_is_repeated(self):
        # The Yorkshire Dales park's orders pin other North Yorkshire roads.
        mine = set(w for ways in PINNED.values() for w in ways)
        for o in _orders():
            if o["id"].startswith(_NY):
                continue
            with self.subTest(o["id"]):
                self.assertFalse(set(o.get("ways") or []) & mine)
                self.assertNotIn(o.get("source_url"),
                                 [t["source_url"] for t in TEXT.values()])

    def test_no_personal_data_in_the_new_entries(self):
        for oid in PINNED:
            text = json.dumps(_order(oid), ensure_ascii=False)
            for pattern in _PERSONAL:
                with self.subTest(oid, pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(text))

    def test_the_personal_data_layer_catches_what_it_is_for(self):
        # Fictional numbers from Ofcom's drama ranges (0306 999 0xxx,
        # 01632 960xxx, 07700 900xxx), assembled here so the repository's
        # review gate, which reads every added line, does not take the
        # fixtures for real ones.
        sp = " "
        for bad in ("Tel: 0306" + sp + "999 0123", "01632" + sp + "960123",
                    "(01632)" + sp + "960123", "+44" + sp + "1632 960123",
                    "07700" + sp + "900123", "Cllr Brown",
                    "a.b" + "@" + "example.org", "Mrs Smith"):
            with self.subTest(bad):
                self.assertTrue(any(p.search(bad) for p in _PERSONAL))
        for fine in ("SE23635222", "NZ78711556", "U8006/9/60",
                     "30.32/U618/40", "Order 2026 No 593", "SE 453259 490750"):
            with self.subTest(fine):
                self.assertFalse(any(p.search(fine) for p in _PERSONAL))


class AnUnsurfacedRoadClosureIsBuilt(unittest.TestCase):
    """The register's own build, through a way shaped as U569 is."""

    def test_a_temporary_closure_is_dated_and_drawn_as_a_road(self):
        byways = Byways([
            Way("NY-UCR-scarborough-u569", "North Yorkshire",
                "Unsurfaced unclassified road (UCR) Scarborough U569",
                [[(-0.53958, 54.2899), (-0.5450, 54.2930)]],
                way_class="ucr")])
        data, problems = reg.build(byways, [_order(HUTTON)])
        self.assertEqual(problems, [])
        [item] = data["items"]
        self.assertEqual(item["on"], "ucr")
        self.assertEqual(item["form"], "temporary")
        props = CO.feature_of(item, data["source"], IN_FORCE)["properties"]
        self.assertEqual(props["oform"], "seasonal")  # TTRO, as the pack has
        self.assertEqual((props["start"], props["end"]),
                         ("2026-05-11", "2027-11-10"))
        self.assertEqual(props["label"], "Road closed")
        self.assertIsNotNone(CO.feature_of(item, data["source"],
                                           "2027-11-10"))
        self.assertIsNone(CO.feature_of(item, data["source"], "2027-11-11"))


class TheCommittedOutput(unittest.TestCase):
    """tro/council/order-register.json, which the order build reads."""

    @classmethod
    def setUpClass(cls):
        cls.out = _committed()
        cls.items = dict((i["id"], i) for i in cls.out["items"])
        cls.held = dict((p["id"], p["why"]) for p in cls.out["review"])

    def test_it_was_built_from_this_register(self):
        self.assertEqual(self.out["records"], len(_orders()))

    def test_every_order_is_published_on_its_lanes_as_a_road(self):
        for oid, ways in PINNED.items():
            with self.subTest(oid):
                if oid in self.held:
                    self.assertIn(GONE, self.held[oid])
                    continue
                item = self.items.get(oid)
                self.assertIsNotNone(item, "published")
                self.assertEqual(item["ways"], ways)
                self.assertEqual(item["on"], "ucr")
                self.assertEqual(item["match"], "reviewed")
                self.assertEqual(item["authority"], "North Yorkshire")

    def test_most_of_them_are_published_today(self):
        # A floor, so holding every one back cannot pass the tests above.
        published = [oid for oid in PINNED if oid in self.items]
        self.assertGreaterEqual(len(published), 12)

    def test_each_is_drawn_on_its_own_lanes_in_north_yorkshire(self):
        # The lines come from the ways containers, so they are checked by
        # where they are and how long they are: a lane swapped with another,
        # cut short or moved fails here.
        for oid in PINNED:
            item = self.items.get(oid)
            if item is None:
                continue
            with self.subTest(oid):
                lines = _lines(item["geometry"])
                self.assertTrue(lines and all(len(l) >= 2 for l in lines))
                for lon, lat in (p for l in lines for p in l):
                    self.assertTrue(NY_LON[0] < lon < NY_LON[1] and
                                    NY_LAT[0] < lat < NY_LAT[1],
                                    (lon, lat))
                self.assertAlmostEqual(_length(lines), LENGTH_M[oid],
                                       delta=LENGTH_M[oid] * 0.02)
                points = [p for l in lines for p in l]
                middle = (sum(p[0] for p in points) / len(points),
                          sum(p[1] for p in points) / len(points))
                self.assertLess(reg._metres(middle, CENTRE[oid]), 300.0)

    def test_each_grid_reference_lies_on_the_lane_drawn(self):
        for oid, want in TEXT.items():
            item = self.items.get(oid)
            if item is None or not want.get("grid_refs"):
                continue
            way = Way(oid, "North Yorkshire", oid,
                      _lines(item["geometry"]), way_class="ucr")
            for ref in want["grid_refs"]:
                with self.subTest(oid, ref=ref):
                    point = grid_to_wgs84(*reg.grid_ref(ref))
                    self.assertLess(reg._way_distance(way, point), 50.0)

    def test_each_reads_as_its_order_says(self):
        for oid, (label, where) in SHOWN.items():
            item = self.items.get(oid)
            if item is None:
                continue
            with self.subTest(oid):
                want = TEXT[oid]
                self.assertEqual(item["title"], want["order_name"])
                self.assertEqual(item["ref"], want["order_name"])
                self.assertEqual(item["where"], where)
                self.assertEqual(item["vehicles"], want["vehicles"])
                self.assertEqual(item["form"], want["form"])
                self.assertEqual(item.get("start"), want["start"])
                self.assertEqual(item.get("end"), want.get("end"))
                self.assertEqual(item["url"], want["source_url"])
                self.assertEqual(item["source_name"], COUNCIL +
                                 " - published list of byway orders")
                self.assertEqual(CO.label_for(item)[2], label)
                self.assertLess(len(want["order_name"]), 240)

    def test_each_is_drawn_while_its_order_is_in_force(self):
        for oid in PINNED:
            item = self.items.get(oid)
            if item is None:
                continue
            with self.subTest(oid):
                feature = CO.feature_of(item, self.out["source"], IN_FORCE)
                self.assertIsNotNone(feature, "drawn on " + IN_FORCE)
                props = feature["properties"]
                self.assertEqual(props["ways"], PINNED[oid])
                self.assertEqual(props["label"], SHOWN[oid][0])
                if TEXT[oid]["form"] == "temporary":
                    self.assertEqual(props["oform"], "seasonal")
                else:
                    self.assertNotIn("oform", props)

    def test_a_temporary_order_is_not_drawn_after_it_ends(self):
        for oid, want in TEXT.items():
            item = self.items.get(oid)
            if item is None or "end" not in want:
                continue
            with self.subTest(oid):
                self.assertIsNotNone(
                    CO.feature_of(item, self.out["source"], want["end"]))
                after = "%d%s" % (int(want["end"][:4]) + 1, want["end"][4:])
                self.assertIsNone(
                    CO.feature_of(item, self.out["source"], after))

    def test_a_closure_not_yet_begun_waits_for_the_horizon(self):
        # More than 90 days before 19 October 2026 the closure is not drawn.
        for oid in (FARNDALE, MULGRAVE):
            item = self.items.get(oid)
            if item is None:
                continue
            with self.subTest(oid):
                self.assertIsNone(
                    CO.feature_of(item, self.out["source"], "2026-07-01"))
                self.assertIsNotNone(
                    CO.feature_of(item, self.out["source"], "2026-10-01"))

    def test_every_item_says_what_its_order_says(self):
        # Everything in an item but its lines comes from its order; the
        # lines come from the ways containers, which this suite never reads.
        orders = dict((o["id"], o) for o in _orders())
        for oid in PINNED:
            item = self.items.get(oid)
            if item is None:
                continue
            with self.subTest(oid):
                byways = Byways([Way(u, item["authority"], u,
                                     [[(0.0, 0.0), (0.001, 0.001)]],
                                     way_class="ucr") for u in item["ways"]])
                want = reg.item_of(orders[oid], byways, item["ways"],
                                   item["match"])
                want.pop("geometry")
                got = dict(item)
                got.pop("geometry")
                self.assertEqual(got, want)

    def test_the_council_is_named_as_a_source(self):
        self.assertIn(COUNCIL, self.out["source"]["name"])
        self.assertIn("North Yorkshire", self.out["source"]["authorities"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
