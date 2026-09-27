#!/usr/bin/env python3
"""Build Trail Blazer's routing profiles from the stock car profile.

    python build_profiles.py --base <brouter>/misc/profiles2 --out dist/profiles

A rider picks how they want to travel, not a cost function. Three answers:

    Fastest      main roads, get there
    Fun          back lanes and bends, still tarmac
    Green lanes  byways and unsurfaced tracks allowed

All three derive from the stock kinematic car model, which already handles
access rules, one-ways, turn costs and speed limits properly. Deriving rather
than hand-writing means an upstream fix reaches all three by re-running this,
and the diff between our profiles stays small enough to actually read.

The kinematic model treats `costfactor` as a penalty added to an energy-based
cost, so a bigger number means "avoid this road".

THE ACCESS RULES ARE NOT UNTOUCHED, and this docstring used to say they were.
The stock chain is a CAR's, and it answered wrongly for a motorbike and a 4x4
on four kinds of way (see GLOBALS, VEHICLE_WAY and VEHICLE_NODE):

  * on Green lanes, any `highway=track` carrying no access tag at all - the
    stock `use_offroad` switch granted it, and on England and Wales that
    planned motor vehicles down private farm tracks and unmarked footpaths;
  * a path, footway, bridleway or cycleway carrying a GENERIC `access=yes` or
    `access=permissive` - the chain reads the highway class only when no
    access tag is present, so the generic tag answered for motor vehicles;
  * a way `motorcar=yes` but closed to motorcycles further down OSM's
    motorcycle hierarchy - the chain asks `motorcar` first and stops;
  * a ford mapped as a NODE, which nothing in the node context read - and
    stepping stones mapped as a node, which it still did not read once point
    fords were taught to it.

Every one of those only ever REFUSES more than the stock chain. Nothing here
opens a way the stock chain refused.

    python build_profiles.py --base <brouter>/misc/profiles2 --check <app>/assets/routing

regenerates in memory and fails if the app's shipped profiles differ from it,
so a hand edit to a .brf (the thing its own header forbids) is caught rather
than silently dropped by the next run.
"""
import argparse
import os
import re
import sys

# The penalty each profile puts on each road class, by OSM highway tag.
#
# Fun is the interesting one. Penalising motorway and trunk heavily while
# leaving tertiary and unclassified at zero is what turns "shortest time" into
# "the way you would actually choose on a Sunday" - but residential carries a
# small penalty too, because a route that threads through housing estates is
# not fun, it is just slow.
CLASS_PENALTIES = {
    "fast": {},
    "fun": {
        "motorway": 12,
        "motorway_link": 12,
        "trunk": 8,
        "trunk_link": 8,
        "primary": 3.0,
        "primary_link": 3.0,
        "secondary": 0.8,
        "secondary_link": 0.8,
        "tertiary": 0.0,
        "tertiary_link": 0.0,
        "unclassified": 0.0,
        "residential": 0.6,
        "living_street": 2.0,
        "service": 3.0,
    },
    # THE TABLE THE APP SHIPS. A "seek unsurfaced ways" table (motorway 14 ...
    # unclassified 1.1, commit 110c705, 2026-09-10) was written here and never
    # reached the app: its trailblazer-lanes.brf has carried this one since
    # 610bce3, and every route and test since was made against it. It is
    # restored here so the generator reproduces what riders actually get
    # (`--check`). Whether Green lanes should charge more for tarmac is a
    # separate decision, to be made on routes, not by a regeneration.
    "lanes": {
        "motorway": 12,
        "motorway_link": 12,
        "trunk": 6,
        "trunk_link": 6,
        "primary": 2.0,
        "primary_link": 2.0,
        "secondary": 0.6,
        "secondary_link": 0.6,
        "tertiary": 0.0,
        "tertiary_link": 0.0,
        "unclassified": 0.0,
        "residential": 0.6,
        "service": 2.0,
    },
}

# Global switches per profile. These are the stock car profile's own knobs.
GLOBALS = {
    "fast": {
        "vmax": "95",
        "avoid_motorways": "false",
        "avoid_unpaved": "true",
        "use_offroad": "false",
        "consider_town": "false",
    },
    "fun": {
        # Lower target speed: the model then stops treating a fast A-road as
        # obviously better than a lane.
        "vmax": "75",
        "avoid_motorways": "true",
        # "Must still be roads" - unpaved is off, and stays off.
        "avoid_unpaved": "true",
        "use_offroad": "false",
        # Bypass towns where it reasonably can.
        "consider_town": "true",
    },
    "lanes": {
        "vmax": "60",
        "avoid_motorways": "true",
        # Unsurfaced ways are timed at trail speed here and only here.
        "avoid_unpaved": "false",
        # FALSE, on Green lanes too. It was true, and it is the stock car
        # chain's "additional roads" switch: `switch and highway=track
        # use_offroad 1`, reached only when a track carries NO access tag of
        # any kind. It granted every such track to a motorbike and a 4x4.
        #
        # In England and Wales a track nothing opens to motors and no byway
        # record holds is most often a private farm or forestry track, or a
        # public footpath or bridleway mapped as a track without its
        # motor_vehicle=no. Riding it is trespass, or an offence under s.34 of
        # the Road Traffic Act 1988. Measured through the real engine over
        # 1,440 road-to-road green-lane routes: 160 km on such tracks, 86
        # routes with more than 500 m of it; nothing warned, because the
        # rights boundary speaks only where a RECORDED byway ends.
        #
        # What still routes: a track any access tag opens (motor_vehicle=yes,
        # motorcar=yes, access=yes, access=permissive, ...), because those are
        # read by the stock chain before the highway class is. What no longer
        # does: a byway OSM maps as a bare track with nothing but a
        # `designation` - and `designation` is not in lookups.dat, so the
        # tiles cannot tell that byway from the farm track beside it. Until
        # the tiles carry the legal tier (PIVOT-PLAN 1.11), `use_offroad`
        # stays a %param% switch: the app may turn it on for a leg it knows
        # runs along a RECORDED byway, and nowhere else.
        "use_offroad": "false",
        "consider_town": "true",
    },
}

TITLES = {
    "fast": "Fastest - main roads, quickest way there",
    "fun": "Fun - back roads and bends, still tarmac",
    "lanes": "Green lanes - byways and unsurfaced tracks allowed",
}

HEADER = """#
# Trail Blazer: {title}
#
# GENERATED by build_profiles.py from the stock car-vario profile.
# Do not edit by hand - edit the generator and re-run, or the next upstream
# refresh will quietly drop your change.
#
# Derived from BRouter's profiles (MIT). The one-way handling, turn costs and
# speed model are the stock ones. The access rules are the stock car chain
# plus the VEHICLE MODEL's refusals below, which only ever refuse more: paths
# are not roads whatever generic tag they carry, a motorbike is asked its own
# access question, and a ford mapped as a point is a ford. What differs
# between our three profiles is the target speed, a handful of switches, and
# a penalty per road class.
#
"""


# ---------------------------------------------------------------------------
# THE VEHICLE MODEL - spec 9.2 "Distinct motorbike and 4x4 profiles"
# ---------------------------------------------------------------------------
# Kept here rather than hand-edited into the three .brf files, because the
# header those files carry says so: "edit the generator and re-run, or the next
# upstream refresh will quietly drop your change". This block IS that change,
# and it is the whole of what makes a trail bike and a Defender come back with
# different routes rather than the same route with different times against it.
#
# The app rewrites only the `assign` lines in VEHICLE_GLOBALS - see
# `lib/data/routing/vehicle_profile.dart`. Everything else below is fixed, so
# the rules live next to the tags they act on and can be read as one piece.
#
# BEFORE ADDING A TAG HERE, CHECK lookups.dat. A profile may only name tags the
# lookup table declares; `BExpression.parse` throws "unknown lookup name" on
# anything else, ProfileCache then fails, and the rider gets NO ROUTING AT ALL.
# maxwidth, maxheight, maxweight, maxlength and width are NOT in the table, so
# there is no clause for a posted limit - the tiles were encoded against this
# same table and never carried one.
VEHICLE_GLOBALS = """# ---------------------------------------------------------------------------
# VEHICLE MODEL - spec 9.2 "Distinct motorbike and 4x4 profiles"
# ---------------------------------------------------------------------------
# Until this block existed, a trail bike and a Defender were handed identical
# costs and came back with the IDENTICAL LINE. Only the clock differed, via
# TravelProfile, which rewrites the kinematic parameters and says in its own
# doc comment that "route CHOICE is untouched". Spec 7.2.1 recorded the
# shortfall by grep: ford 0 hits, maxweight 0, maxwidth 0, height 0,
# smoothness 0 across all three profiles.
#
# WHAT THE ENGINE CAN AND CANNOT SEE, and why width and weight look the way
# they do below. A profile may only name tags that assets/routing/lookups.dat
# declares: BExpression.parse throws "unknown lookup name" on anything else,
# ProfileCache then fails to parse the profile, and NOTHING routes at all.
# Checked against the shipped lookups.dat (lookupversion 11, minorversion 2):
#
#   present, way context  : ford, smoothness, tracktype, surface, highway,
#                           motorcycle, motorcar, motor_vehicle, vehicle,
#                           access, incline, mtb:scale, sac_scale, obstacle
#   present, node context : barrier, ford, highway
#   ABSENT ENTIRELY       : maxwidth, maxheight, maxweight, maxlength, width
#
# The absent five are why width and height below act on BARRIER CLASSES and
# their typical apertures rather than on a way's own posted limit, and why
# there is no weight clause at all. It is not an oversight in the profile:
# the lookup table is the dictionary the .rd5 tiles were ENCODED with, so a
# limit that is not in it was never carried into the routing graph and no
# profile can reach it. Naming the tags here without rebuilding the tiles
# would be worse than leaving them out - the clause would parse, evaluate
# against an always-absent tag, and silently never fire.
assign vehicle_is_4x4      = false  # %vehicle_is_4x4% | Route for a 4x4 rather than a motorbike | boolean
assign vehicle_width       = 0.9    # %vehicle_width% | Vehicle width in metres | number
assign vehicle_height      = 1.4    # %vehicle_height% | Vehicle height in metres | number

assign avoid_fords         = false  # %avoid_fords% | Avoid water crossings | boolean
assign avoid_gates         = false  # %avoid_gates% | Avoid gates | boolean
assign avoid_steps         = true   # %avoid_steps% | Avoid steps | boolean
assign avoid_narrow        = false  # %avoid_narrow% | Avoid narrow ways | boolean
"""

VEHICLE_WAY = """# --- VEHICLE MODEL, way half ----------------------------------------------
#
# The only place in this file where the two vehicles are told apart on the
# LINE rather than on the clock. See the header block in ---context:global for
# which tags the engine can actually see.

# EXPLICIT DENIAL, for the one vehicle the stock chain cannot see.
#
# The stock access chain is a CAR's: it consults motorcar, then motor_vehicle,
# then vehicle, then access, stops at the first tag it finds, and NEVER reads
# motorcycle. So a way tagged `motorcycle=no` routed a motorbike straight down
# it - lookups.dat's own sample counts 92,079 ways carrying `motorcycle=no`
# against 27,978 carrying `motorcycle=yes` - and so did a way where CARS are
# let through and motorcycles are not: `access=private motorcar=yes`, or
# `motor_vehicle=private motorcar=permissive`. The chain found `motorcar`,
# said yes, and never read another tag. Measured on some seven thousand
# motorbike routes through the real engine: rare (one mid-route stretch, near
# Pickering), and still a private way the rider had no business on.
#
# So for a motorbike this asks the MOTORCYCLE's own question, down OSM's own
# hierarchy for it: motorcycle, then motor_vehicle, then vehicle, then access,
# the most specific tag present deciding. `motorcar` is not in that hierarchy
# and is not read.
#
# THERE IS NO 4x4 HALF TO THIS CLAUSE, and that is not an omission. The stock
# chain is already the 4x4's own hierarchy (motorcar -> motor_vehicle ->
# vehicle -> access), so a second copy of it here would be a rule that can
# never change an answer - and a rule that cannot change an answer is worse
# than no rule, because it reads like cover.
#
# The test is the stock chain's own, inverted: a value that is PRESENT and is
# not one of its four permissive ones is a denial. An ABSENT tag is not. It
# means the source said nothing, which is neither permission nor prohibition,
# and the stock verdict stands untouched. Written as "not one of the four"
# rather than as a list of refusals on purpose: lookups.dat folds `forestry`,
# `delivery` and others into aliases, and naming an alias fails to parse -
# which is NO ROUTING AT ALL, not a missed clause.
#
# Nothing here ever ALLOWS a way the stock chain refused. Granting a motorbike
# passage on `motorcycle=yes` where `motorcar=no` was considered and rejected:
# this app draws legal conclusions, and its own dataset - not an OSM access
# tag - is the authority on whether a motorbike may use a way.
assign tb_motorcycle_permits =
  switch motorcycle=
    switch motor_vehicle=
      switch vehicle=
        switch access= true
        access=yes|permissive|designated|destination
      vehicle=yes|permissive|designated|destination
    motor_vehicle=yes|permissive|designated|destination
  motorcycle=yes|permissive|designated|destination

assign tb_denied_here =
  if vehicle_is_4x4 then false
  else not tb_motorcycle_permits

# PATHS ARE NOT ROADS, whatever generic tag they carry.
#
# The stock chain reads the highway class only when NO access tag is present:
# `switch access= <highway list> access=yes|permissive|designated|destination`.
# So the moment a path carries `access=permissive` - how a landowner's
# permissive FOOTPATH is mapped in Britain - or a bridleway `access=yes`, the
# generic tag answers for every mode and the fact that the way is a path is
# never looked at. The footpath was a road to a motorbike and a 4x4 alike, on
# every style including the default Fastest. Measured through the real engine:
# 18.8 km of `highway=path access=permissive` over 1,440 road-to-road routes,
# and a Fastest route across Dartmoor that was a third permissive path.
#
# A motor vehicle on a footpath or bridleway is an offence under s.34 of the
# Road Traffic Act 1988, and on a permissive path it is trespass on the land of
# an owner who gave leave to walkers. The steps backstop below reasoned the
# same way about one class and stopped there.
#
# What opens one of these ways to a motor vehicle is a tag that NAMES this
# vehicle, or motor vehicles in general, and says it may: a bridleway that is
# also a byway and says `motor_vehicle=yes`. Never `access` or `vehicle`,
# which a bicycle satisfies too. Steps are not here: they have their own
# switch, `avoid_steps`, which the rider sets.
assign tb_motor_named =
  if vehicle_is_4x4 then
    or motorcar=yes|permissive|designated|destination
       motor_vehicle=yes|permissive|designated|destination
  else
    or motorcycle=yes|permissive|designated|destination
       motor_vehicle=yes|permissive|designated|destination

assign tb_nonmotor_way =
  and highway=path|footway|bridleway|cycleway|pedestrian
      not tb_motor_named

# STEPS, and why the option is not a no-op.
#
# The highway chain in `caraccess` never lists `steps`, so on its own a
# stepped way is already unreachable. But that chain is only consulted when
# there is no `access` tag - `switch access= <highway chain> access=yes|...` -
# so a stepped way carrying `access=yes` short-circuits it and is handed to the
# router as an ordinary road. This is the backstop for that case.
assign tb_steps_blocked = and avoid_steps highway=steps

# NARROW WAYS - A PROXY, AND SAID SO.
#
# There is no `width` and no `maxwidth` in lookups.dat, so a way's running
# width cannot be read at all. What can be read is its CLASS, and a path, a
# bridleway, a footway or a cycleway is under two metres far more often than
# not. A driver who knows their vehicle is 2.1 m wide is better served by that
# proxy than by nothing - but it IS a proxy, and it will avoid a wide
# hard-packed bridleway along with the narrow ones. Off by default for that
# reason; the rider turns it on.
assign tb_is_narrow_class = highway=path|bridleway|footway|cycleway

assign tb_narrow_blocked = and avoid_narrow tb_is_narrow_class

# STEPPING STONES are a footpath crossing. No motor vehicle of any width gets
# over one, so this is a block rather than a cost, and it is not behind
# `avoid_fords`: a rider who is happy to wade is still not riding stepping
# stones.
assign tb_vehicle_blocked =
  or tb_denied_here
  or tb_nonmotor_way
  or tb_steps_blocked
  or tb_narrow_blocked
  or ford=stepping_stones
  and avoid_fords ford=yes

# FORDS, when they are not avoided outright.
#
# The clearest single difference between the two vehicles, and the asymmetry
# is the point. A 4x4's wading depth is set by its air intake, and a Defender's
# is around half a metre. A trail bike's airbox sits lower and its exhaust
# lower still, and a bike that takes water in is not a wet rider, it is a
# recovery and the end of the day. So the same ford is a shrug in a 4x4 and a
# real decision on a bike.
#
# Not a block for either. Fords are a product-maker (spec 9.6 G) and plenty of
# them are ankle-deep; `avoid_fords` above is how a rider says otherwise.
assign tb_ford_penalty =
  if ford=yes then ( if vehicle_is_4x4 then 3 else 9 )
  else 0

# SMOOTHNESS: A JUDGEMENT, NOT A MEASUREMENT.
#
# Flagged as such because everything else in this file that carries a number
# cites something, and this does not. Nobody has measured our riders against
# OSM's smoothness bands.
#
# What is argued: a trail bike picks a line through ruts a 4x4 has to
# straddle, and where it cannot, the rider dabs and rides on; a 4x4 that
# bellies out in the same rut is stuck and waiting for a strap. So the bike
# tolerates roughly one band more than the 4x4, and this table is that one
# band of offset and no more. If it is ever measured, this is the thing to
# replace.
assign tb_smoothness_penalty =
  if      smoothness=impassable    then ( if vehicle_is_4x4 then 60 else 30 )
  else if smoothness=very_horrible then ( if vehicle_is_4x4 then 30 else 12 )
  else if smoothness=horrible      then ( if vehicle_is_4x4 then 12 else 5 )
  else if smoothness=very_bad      then ( if vehicle_is_4x4 then 5 else 2 )
  else if smoothness=bad           then ( if vehicle_is_4x4 then 2 else 0.5 )
  else 0

# WIDTH, where it can act on a way rather than on a barrier.
#
# 1.6 m is the line between a machine you sit on and one you sit in: a Jimny
# is 1.645 m across the body, a Defender 90 is 1.79 m, a Land Cruiser 1.98 m,
# and a trail bike is about 0.9 m across the bars. Above that line a
# bridleway-width track is a bad afternoon even when it is legal and even when
# `avoid_narrow` is off, so it costs something rather than nothing.
assign tb_width_penalty =
  if tb_is_narrow_class then ( if greater vehicle_width 1.6 then 8 else 0 )
  else 0

assign tb_vehicle_penalty =
  add tb_ford_penalty
  add tb_smoothness_penalty
  tb_width_penalty

"""

VEHICLE_NODE = """# --- VEHICLE MODEL, node half ---------------------------------------------
#
# WIDTH AND HEIGHT ACT HERE, on barrier classes and their typical apertures,
# because lookups.dat carries no maxwidth and no maxheight and the tiles
# therefore carry no posted limit anywhere. A typical aperture is a weaker
# instrument than a surveyed one, so it is used in ONE DIRECTION ONLY: it can
# stop a vehicle that certainly does not fit, and it never lets one through
# that the stock rules refused.

# Barriers a rider opens and rides through.
#
# A gate on a byway is a thing you get off and open, not a wall - and the
# stock rule charges 1,000,000 for every one of them, because `caraccess`
# below is false at any barrier node carrying no access tag. On a green-lane
# network, where gates are the normal furniture, that is a hard refusal of
# most of the network for the sake of a thing that costs a minute.
assign tb_is_gate = barrier=gate|lift_gate|swing_gate|bump_gate|hampshire_gate|sliding_gate|footgate|chain|rope|bar

# Nothing on this node says who may pass; the gate is the only obstacle.
# Where an access tag DOES speak - `access=private`, `motor_vehicle=no` - the
# stock verdict stands and none of this fires.
assign tb_access_silent = and motorcar= and motor_vehicle= and vehicle= access=

# What a gate costs, in the engine's cost units, which are metres of
# equivalent distance.
#
# Stop, open, ride or drive through, close: about 45 s on a bike, and call it
# 90 s in a 4x4, where it is usually the driver getting out twice and the
# vehicle is harder to leave standing in a gateway. At the 30 km/h a green
# lane is actually covered at - 8.3 m/s - that is 375 m and 750 m. Rounded.
assign tb_gate_cost = if vehicle_is_4x4 then 800 else 400

# BARRIERS NOTHING GETS PAST.
#
# Every one of these is currently FREE to the router. The stock node rule
# names only gate, bollard, lift_gate and cycle_barrier, so a stile, a fallen
# tree, a concrete block or a set of spikes across a lane cost nothing at all
# and the route went straight through them.
#
# A motorcycle_barrier is in the list for both vehicles on purpose: it is
# built to stop the narrower of the two, so it stops the wider one as well.
assign tb_barrier_impassable =
  barrier=stile|kissing_gate|turnstile|horse_stile|full-height_turnstile|motorcycle_barrier|block|log|tree|fallen_tree|windfall|debris|spikes|wall|fence|hedge|ditch

# WIDTH AS A HARD CONSTRAINT, against typical apertures in metres:
#
#   chicane   ~1.0 m   a bike threads it; nothing car-bodied does
#   bus_trap  ~1.6 m   built to stop a car body; a bike rides the kerbs
#
# A vehicle wider than the aperture is stopped. A narrower one is NOT waved
# through anything: cycle_barrier and bollard stay in the stock blocked list
# either way, because relaxing those would route a motorbike past a line of
# bollards that may well be a closure, and this app does not guess in that
# direction.
assign tb_too_wide_here =
  or ( and barrier=chicane   greater vehicle_width 1.0 )
     ( and barrier=bus_trap  greater vehicle_width 1.6 )

# HEIGHT. One barrier class in the whole table carries it. A height restrictor
# is a bar, and the common British one over a lane end or a car park is set at
# 6 ft 6 in, which is 1.98 m.
assign tb_too_tall_here = and barrier=height_restrictor greater vehicle_height 1.98

# FORDS MAPPED AS A POINT.
#
# The way half sees a ford only where the ROAD ITSELF is tagged `ford=yes`.
# The commoner way OSM maps one is a single NODE, `ford=yes`, where the stream
# crosses the road: the shipped lookups.dat counts 37,927 ford nodes against
# 20,552 ford ways, and on real routes through the engine 584 ford nodes were
# crossed against 100 ford ways. Until this, nothing in the node context read
# `ford`, so "Avoid fords" - "This refuses them outright" - drove straight
# through most fords, and a bike paid nothing more than a 4x4 for them.
#
# The refusal is in `initialcost` below. The cost, when fords are allowed,
# mirrors the way half's asymmetry (9 per metre on a bike, 3 in a 4x4) over
# the 25 m or so a ford way typically runs: 225 m and 75 m of equivalent
# distance. That length is a JUDGEMENT, not a measurement - the ratio between
# the two vehicles is the part carried over from the way half.
assign tb_node_ford_cost =
  if ford=yes then ( if vehicle_is_4x4 then 75 else 225 )
  else 0

# STEPPING STONES MAPPED AS A POINT are refused in `initialcost` below,
# outright and whatever `avoid_fords` says, exactly as the way half refuses
# `ford=stepping_stones`: no motor vehicle of any width gets over one. The
# lookup table carries the value on nodes too (310 of them), and until this a
# stepping-stones node on a way the profile otherwise opened was priced like
# any other point and routed over. `tb_node_ford_cost` does not cost them
# because it never meets one: the refusal comes first.

"""

OLD_NODE_INITIALCOST = """assign initialcost =
       switch and avoid_toll barrier=toll_booth 1000000
       switch caraccess
              0
              1000000
"""

NEW_NODE_INITIALCOST = """# Order matters, and it is not the order the clauses were written in.
#
# The hard refusals come FIRST, before `caraccess`, because a node can carry
# both `access=yes` and a stile: the stock rule would read the access tag, say
# yes and hand the router a route through a piece of footpath furniture.
# Stepping stones are one of them, not behind `avoid_fords`: a rider who is
# happy to wade is still not riding stepping stones.
# `avoid_gates` and `avoid_fords` are above `caraccess` for the same reason - a
# rider who asked not to be sent through gates or fords means it whether or not
# the node is signed open.
#
# The gate cost comes LAST, and only where nothing else spoke: it is the one
# clause here that makes the router MORE willing than the stock profile, so it
# is reachable only after every refusal has had its say. A point ford's cost
# rides on whichever of the two lets the node through.
assign initialcost =
       switch and avoid_toll barrier=toll_booth 1000000
       switch tb_barrier_impassable                 1000000
       switch tb_too_wide_here                      1000000
       switch tb_too_tall_here                      1000000
       switch ford=stepping_stones                  1000000
       switch and avoid_narrow barrier=chicane      1000000
       switch and avoid_gates tb_is_gate            1000000
       switch and avoid_fords ford=yes              1000000
       switch caraccess                             tb_node_ford_cost
       switch and tb_access_silent tb_is_gate       add tb_gate_cost tb_node_ford_cost
       1000000
"""


def insert_vehicle_model(text):
    """Add the vehicle block, and hook it into the two cost functions.

    Every anchor is checked. A silently-skipped substitution here ships three
    profiles that parse perfectly and route a Defender as a motorbike, which is
    invisible from the outside: the line still draws, and only the rider on the
    wrong side of a chicane ever finds out.
    """
    def once(haystack, needle, replacement, what):
        if haystack.count(needle) != 1:
            sys.exit("FATAL: %d matches for the %s anchor; fix the generator."
                     % (haystack.count(needle), what))
        return haystack.replace(needle, replacement, 1)

    kinematic = "# Kinematic model parameters"
    text = once(text, kinematic, VEHICLE_GLOBALS + "\n" + kinematic,
                "kinematic-block")
    text = once(text, "assign tb_class_penalty =",
                VEHICLE_WAY + "assign tb_class_penalty =", "class-penalty")
    text = once(text, "  add tb_class_penalty\n",
                "  add tb_class_penalty\n  add tb_vehicle_penalty\n",
                "cost-chain")
    text = once(text, "  else if is_avoided_toll_road then 10000\n",
                "  else if is_avoided_toll_road then 10000\n"
                "  else if tb_vehicle_blocked then 10000\n",
                "costfactor-head")
    text = once(text, OLD_NODE_INITIALCOST,
                VEHICLE_NODE + NEW_NODE_INITIALCOST, "node-initialcost")
    return text


# THE TRAVEL MODEL the app ships (greenroadmap-app e6c8b2e, "Ask what the
# journey is on"). It was made in the .brf files by hand and never here, so a
# run of this generator put the car's 1,640 kg and its walking-pace off-tarmac
# speed caps back: grade3-5 track at 1 km/h, an unpaved surface at 2 km/h. The
# app rewrites these per journey (`travel_profile.dart`, which RAISES on an
# anchor it cannot find), so they are the anchors it expects as well as the
# defaults a motorbike is timed with. Route CHOICE reads maxspeed only through
# `equal maxspeed 0`, so none of this moves a line - it moves the clock.
TRAVEL_MODEL = [
    ("assign totalweight      = 1640   # %totalweight% | Total weight of the car (in kg) | number\n"
     "assign f_roll           = 232    # %f_roll% | Rolling friction (in Newton) | number\n"
     "assign f_air            = 0.4    # %f_air% | Drag force (in Newton / (m/s)^2), 0.5*cw*A*rho | number\n",
     "assign totalweight      = 300    # %totalweight% | Total weight of bike + rider + kit (in kg) | number\n"
     "assign f_roll           = 60     # %f_roll% | Rolling friction (in Newton) | number\n"
     "assign f_air            = 0.28   # %f_air% | Drag force (in Newton / (m/s)^2), 0.5*cw*A*rho | number\n",
     "kinematic-weight"),
    ("assign p_standby        = 250    # %p_standby% | Watt | number\n",
     "assign p_standby        = 0      # %p_standby% | Watt | number\n",
     "kinematic-standby"),
    ("  switch surface=fine_gravel ( switch avoid_unpaved 3 10 )\n"
     "  switch avoid_unpaved 1 2\n",
     "  switch surface=fine_gravel ( switch avoid_unpaved 20 45 )\n"
     "  switch avoid_unpaved 15 30\n",
     "maxspeed-surface"),
    ("  switch tracktype=grade1 40\n"
     "  switch tracktype=grade2 ( switch avoid_unpaved 2 5 )\n"
     "  1\n",
     "  switch tracktype=grade1 50\n"
     "  switch tracktype=grade2 ( switch avoid_unpaved 25 40 )\n"
     "  switch tracktype=grade3 ( switch avoid_unpaved 18 30 )\n"
     "  switch tracktype=grade4 ( switch avoid_unpaved 12 20 )\n"
     "  switch avoid_unpaved 10 15\n",
     "maxspeed-tracktype"),
]


def insert_travel_model(text):
    """Replace the car's physics and off-tarmac caps with the shipped ones."""
    for old, new, what in TRAVEL_MODEL:
        if text.count(old) != 1:
            sys.exit("FATAL: %d matches for the %s anchor; fix the generator."
                     % (text.count(old), what))
        text = text.replace(old, new, 1)
    return text


def penalty_expression(penalties):
    """Build the profile-language expression for our road-class penalty."""
    if not penalties:
        return "assign tb_class_penalty = 0\n"

    lines = ["assign tb_class_penalty ="]
    first = True
    for tag, value in penalties.items():
        keyword = "if" if first else "else if"
        lines.append("  %s ( highway=%s ) then %s" % (keyword, tag, value))
        first = False
    lines.append("  else 0")
    return "\n".join(lines) + "\n"


def apply_globals(text, overrides):
    """Rewrite `assign name = value` in the global block."""
    for name, value in overrides.items():
        pattern = re.compile(
            r"^(assign\s+%s\s+)=\s*\S+" % re.escape(name), re.M)
        new, count = pattern.subn(
            lambda m: "%s= %s" % (m.group(1), value), text, count=1)
        if count == 0:
            sys.exit("FATAL: could not find `assign %s` to override. The base "
                     "profile has changed shape; fix the generator." % name)
        text = new
    return text


def insert_penalty(text, penalties):
    """Define our penalty and add it into the cost function."""
    anchor = "assign costfactor ="
    if anchor not in text:
        sys.exit("FATAL: no `assign costfactor` in the base profile.")
    text = text.replace(anchor, penalty_expression(penalties) + "\n" + anchor, 1)

    # The cost function is a chain of `add` terms ending in a bare 0. Hooking
    # onto the last named term keeps us inside that chain rather than guessing
    # at its shape.
    tail = "  add no_river_penalty"
    if tail not in text:
        sys.exit("FATAL: the cost chain has changed shape; fix the generator.")
    return text.replace(tail, tail + "\n  add tb_class_penalty", 1)


def profile_text(base, key):
    """One profile, as the app ships it.

    `insert_vehicle_model` was written and never called from here, so this
    generator's output had no vehicle model at all while the app's profiles
    carried one by hand. Every step is now applied, in one place, and
    `--check` holds the result against the app's files.
    """
    text = insert_penalty(base, CLASS_PENALTIES[key])
    text = insert_vehicle_model(text)
    text = insert_travel_model(text)
    text = apply_globals(text, GLOBALS[key])
    return HEADER.format(title=TITLES[key]) + text


def read_base(base_dir):
    base_path = os.path.join(base_dir, "car-vario.brf")
    if not os.path.isfile(base_path):
        sys.exit("no car-vario.brf in %s" % base_dir)
    # Universal newlines (the default), so a CRLF checkout of the stock file
    # still meets the anchors above, which are all written with \n.
    with open(base_path, encoding="utf8") as fh:
        return fh.read()


def check(base_dir, app_dir):
    """Exit non-zero if the app's shipped profiles are not this generator's.

    Compared with line endings normalised, because git may check the app's
    files out either way; any other byte is a difference.
    """
    base = read_base(base_dir)
    bad = []
    for key in ("fast", "fun", "lanes"):
        name = "trailblazer-%s.brf" % key
        path = os.path.join(app_dir, name)
        if not os.path.isfile(path):
            bad.append("%s: missing" % name)
            continue
        with open(path, encoding="utf8") as fh:
            shipped = fh.read().replace("\r\n", "\n")
        if shipped != profile_text(base, key):
            bad.append("%s: differs from the generator" % name)
    for b in bad:
        print("  " + b)
    if bad:
        sys.exit("FATAL: the app's profiles are not what this generator "
                 "builds. Edit the generator and re-run it; never the .brf.")
    print("  the app's three profiles are the generator's")


def build(base_dir, out_dir):
    base = read_base(base_dir)

    os.makedirs(out_dir, exist_ok=True)

    # The lookup table the profiles compile against. Ships with them or
    # nothing routes.
    lookups = os.path.join(base_dir, "lookups.dat")
    if os.path.isfile(lookups):
        with open(lookups, "rb") as src:
            data = src.read()
        with open(os.path.join(out_dir, "lookups.dat"), "wb") as dst:
            dst.write(data)
        print("  lookups.dat            %6.1f KB" % (len(data) / 1024))

    for key in ("fast", "fun", "lanes"):
        text = profile_text(base, key)
        name = "trailblazer-%s.brf" % key
        # LF on every platform: the app's copies are LF, and a CRLF build
        # would differ from them in every line.
        with open(os.path.join(out_dir, name), "w", encoding="utf8",
                  newline="\n") as fh:
            fh.write(text)
        print("  %-22s %6.1f KB" % (name, len(text) / 1024))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True,
                    help="brouter misc/profiles2 directory")
    ap.add_argument("--out", default="dist/profiles")
    ap.add_argument("--check", metavar="APP_ROUTING_DIR",
                    help="compare against the app's assets/routing instead "
                         "of writing anything")
    args = ap.parse_args()
    if args.check:
        print("checking routing profiles...")
        check(args.base, args.check)
        return
    print("building routing profiles...")
    build(args.base, args.out)


if __name__ == "__main__":
    main()
