# Realistic ADS-B and MLAT Reception Ranges for Home Receivers

## Executive Summary

This report summarizes real‑world evidence on maximum and typical reception ranges for hobbyist ADS‑B receivers and MLAT position fixes, and documents software‑level range filters and community practices for handling bogus MLAT positions.
Evidence comes primarily from FlightAware and other hobbyist forums, vendor measurements, and documentation for dump1090, readsb, and associated tooling.[^1][^2][^3][^4]
In ideal terrain, line‑of‑sight physics limits direct 1090 MHz reception to roughly 250 nm (≈460 km) at airliner cruise altitudes, with occasional anomalous propagation extending beyond this; most well‑sited home receivers report maximums around 180–250 nm and average effective ranges closer to 100–150 nm.[^5][^3][^4][^6]
MLAT position quality is dominated by geometry and receiver timing; FlightAware’s implementation estimates typical MLAT errors on the order of several hundred meters and discards solutions whose estimated error exceeds 4 km.[^7]


## 1. Physics‑Based Maximum ADS‑B Range

### 1.1 Theoretical line‑of‑sight limit

A widely cited FlightAware forum post explains that 1090 MHz ADS‑B is effectively line‑of‑sight; with flat terrain the geometry alone limits range to about 250 nm (≈450 km) for aircraft at typical cruise altitudes.[^5]
The same post notes that atmospheric refraction can extend practical radio line‑of‑sight by 50–100 nm beyond the pure geometric horizon, depending on conditions, but terrain almost always reduces range below this ideal.[^5]

Independent antenna vendors advertise similar theoretical maxima: a commercial 1090 MHz collinear antenna marketed for ADS‑B reception claims reception of aircraft “up to 250 km / 400 km” when installed with a clear view.[^8]
A measurement‑based review of the FlightAware antenna using an RTL‑SDR measured a maximum range of 232 nmi (≈430 km), and comments that the “maximum possible physical range” for ADS‑B is approximately 400–600 km, consistent with line‑of‑sight plus some refraction.[^3]

### 1.2 Practical implications

These sources imply that:

- With good siting (high mast, clear horizon), the hard geometric ceiling for 1090 MHz air‑to‑ground reception at cruise altitudes is around 250 nm, and exceptional cases may reach 300 nm or slightly more under favorable propagation.
- In real terrain, obstacles and local clutter typically reduce the achievable maximum below this limit, and the distance varies strongly by azimuth.


## 2. Real‑World ADS‑B Ranges from Hobbyists

### 2.1 FlightAware forum reports

A long‑running FlightAware thread “What is the Maximum Range I can Get?” aggregates examples where users overlay theoretical heywhatsthat.com horizons with their dump1090 range plots; one contributor reports that the 35 000 ft contour from that tool “correlates closely” with their observed maximum‑range ring, reinforcing the ~250 nm physical ceiling discussed above.[^5]
In the same discussion, another user notes that their heywhatsthat 10 000 ft and 30 000 ft rings line up well with 24‑hour tar1090 persistence plots, indicating that in practice most aircraft at cruise are seen out to the terrain‑limited line‑of‑sight distance but rarely beyond.[^5]

In a separate FlightAware discussion, a user running a simple cantenna in a loft, ~35 m above ground, initially recorded a maximum range of 155 nm with an average range around 100 nm per day.[^9]
After optimizing feedline length and moving the receiver into the loft to reduce cable loss, they achieved consistent maximum ranges around 165–175 nm, with rare anomalous decodes beyond 300 nm that they treat as exceptions.[^9]

Another FlightAware thread on antenna performance describes a home‑built ground‑plane antenna mounted indoors providing “solid reception out to about 150 nm” with occasional hits “over 200 nm” away; moving to an external dipole gives “up to 200 nm” range, and adding an LNA yields roughly 10 % range improvement.[^6]
In a separate community comparison of a FlightAware commercial antenna vs a 4‑element coaxial collinear (CoCo), one experimenter measured a maximum range of 181.1 nm (FlightAware antenna) vs 172.6 nm (CoCo), with average ranges of 105.0 nm and 99.6 nm respectively.[^4]

A FlightAware stats‑page discussion notes that FlightAware’s “positions by distance” histogram is computed in statute miles, even when the UI labels the axis as nautical miles; for one user with an actual geometric maximum around 233 nm, this explains why the histogram shows a significant number of hits “over 250” when interpreted as nautical miles.[^10]
That discussion implicitly confirms that well‑sited stations do see a tail of traffic around 230 nm (≈268 statute miles) but not meaningfully beyond.[^10]

### 2.2 Other community reports

A detailed antenna comparison by an independent blogger shows, for an east‑facing indoor installation with clutter, that a FlightAware antenna typically reaches maximum ranges around 181 nm and average ranges near 105 nm.[^4]
The same article emphasizes that optimizing antenna placement and minimizing obstructions dominate over small gain differences, and that multi‑day collection is required to get stable statistics.[^4]

Reddit and other hobbyist communities report similar numbers: one /r/RTLSDR user with an RTL dongle and 1090 MHz antenna 20 ft above ground on a hill achieves “approximately 300 miles” (≈260 nm) maximum range under favorable conditions, while another /r/ADSB user reports sustaining roughly 250 nm maximum range with a good antenna and gain settings before hitting diminishing returns.[^11][^12]
Such reports are consistent with the FlightAware and independent measurements summarized above.


### 2.3 Empirical envelope for home ADS‑B receivers

From these diverse accounts, the practical envelope for hobbyist receivers using RTL‑SDR‑class hardware and decent antennas can be summarized as follows:

- **Typical maximum range (good outdoor install, clear sectors):**
  - 150–200 nm for most azimuths, with best directions occasionally reaching 200–230 nm.[^3][^6][^4][^5]
- **Extreme but plausible maxima:**
  - 230–260 nm under good propagation and geometry, supported by FlightAware comparisons and independent receiver measurements.[^10][^3][^4]
- **Indoor / sub‑optimal installs:**
  - 75–150 nm maximum range is common when antennas are indoors, partially shielded, or mounted low.[^6][^9]

These figures align with the 250 nm theoretical ceiling and vendor claims of up to ~400 km (216 nm) realistic coverage for properly mounted 1090 MHz antennas.[^8][^3][^5]


## 3. MLAT Range and Accuracy

### 3.1 MLAT position accuracy and error filtering

FlightAware’s MLAT implementation is documented in a technical forum post on “How accurate is MLAT?”.
The MLAT solver uses timing from multiple receivers; for a properly located and synchronized receiver, clock errors contribute roughly 200–300 m of pseudorange error per site.[^7]
With several receivers participating, typical position errors after filtering are estimated at ±500 m or better in well‑covered areas.[^7]

Crucially, the FlightAware implementation contains an explicit quality threshold: “The filtering process doesn’t produce results if it estimates the current error at >4 km,” meaning candidate MLAT solutions with an internal error estimate exceeding 4 km are discarded rather than emitted as positions.[^7]
This provides a firm, documented upper bound on MLAT positional uncertainty in their system under normal operation.

A separate FlightAware thread on low‑altitude MLAT artifacts notes that MLAT solutions may occasionally jump several miles when only the minimum four receivers see the target and one site has a mis‑configured location or unstable clock; these are treated as geometry or timing problems rather than an inherent range limit, and the general advice is that “MLAT is a bonus” and occasional inaccuracies are acceptable for Mode S‑only targets.[^13]

### 3.2 Geometric and network constraints on MLAT range

FlightAware contributors explain that MLAT accuracy depends strongly on receiver spacing: with RTL‑SDR‑class timing, pseudorange errors are on the order of 0.3 km, so receivers should be separated by at least ~3 km so that those errors remain a small fraction of the baseline.[^14]
If receivers are too close together, geometry is poor and accuracy degrades; if they are too far apart, overlapping coverage at low altitude disappears, limiting MLAT usefulness near the ground.[^14]

Academic and engineering literature on MLAT and wide‑area MLAT (WAM) systems indicates that wide‑area systems target “area surveillance of up to 400 km” radius, with the understanding that each signal must be received by at least three or four stations and that geographical obstacles make such coverage challenging; this is cited as a limitation of MLAT compared to alternative k‑NN‑based localization methods.[^15]
Commercial MLAT vendors for airport‑surface and local terminal areas typically advertise coverage radii around 30 nm for surface systems, reflecting a deliberate design choice rather than a protocol limit.[^16][^17]

### 3.3 Practical MLAT range seen by hobbyists

Hobbyist MLAT range is generally bounded by the underlying ADS‑B/Mode S signal visibility at participating receivers: if an aircraft is beyond line‑of‑sight for some stations, it will not generate sufficient synchronized messages for MLAT.
Open‑source MLAT client documentation does not impose an explicit maximum distance between aircraft and receivers, focusing instead on timing quality and consistent 12 MHz clocks, so in principle MLAT positions can be computed anywhere an adequate geometric constellation exists.[^18][^19]

In practice, hobbyist MLAT tracks on ADSBexchange and FlightAware maps rarely exceed the same 200–250 nm envelope as direct ADS‑B, because beyond that distance too few receivers see the target simultaneously for stable solutions.
Community threads discussing MLAT coverage rarely report MLAT‑only tracks significantly beyond the local ADS‑B maximum range, and guidance focuses on improving synchronization and receiver geometry rather than extending range.[^20][^21]


## 4. Built‑In Range Filters in dump1090 and readsb

### 4.1 dump1090 (mutability / dump1090‑fa)

The dump1090‑mutability source initializes a `Modes.maxRange` parameter to `1852 * 300`, setting a default maximum range of 300 nautical miles in meters.[^22]
The command‑line help describes the `--max-range` option as:

> `--max-range <distance> Absolute maximum range for position decoding (in nm, default: 300)`[^22]

This limit is applied during position decoding and CPR processing; positions whose inferred range from the receiver exceeds `maxRange` are rejected as implausible.[^22]
FlightAware’s dump1090‑fa configuration examples commonly set `--max-range 360`, slightly increasing the absolute limit to 360 nm (~667 km), but the default code‑level setting remains 300 nm.[^23][^22]

### 4.2 readsb (wiedehopf fork)

The Debian manpage for readsb likewise documents a `--max-range=<dist>` option:

> "Absolute maximum range for position decoding (in nm, default: 300)"[^20]

wiedehopf’s help header confirms the same semantics, describing `max-range` as an “absolute maximum range for position decoding (in nm, default: 300)”.[^24]

Several container stacks that wrap readsb (e.g., sdr‑enthusiasts’ docker‑adsb‑ultrafeeder) expose `READSB_MAX_RANGE` as an environment variable, mapping directly to `--max-range` and sometimes defaulting it to higher values such as 450 nm to be conservative in aggregation contexts.[^25][^26]

### 4.3 tar1090 and derived tooling

Tar1090 itself is primarily a visualization layer and does not hard‑limit range, but many deployments combine it with readsb configured with `--max-range`, so the decoder will have already dropped any positions beyond the configured distance.
Tar1090 also supports heywhatsthat range overlays; forum users report that adding 10 000 ft, 20 000 ft, 30 000 ft, and 40 000 ft theoretical contours shows that the maximum range ring almost always sits near the 35 000–40 000 ft line, reinforcing the ~250 nm limit baked into the default max‑range.[^5]


## 5. Software‑Level Filters for Bogus or Outlier Positions

### 5.1 readsb JSON reliability and position persistence

wiedehopf’s readsb includes explicit filters designed to suppress questionable positions before they reach JSON outputs and web clients.
The `--json-reliable <n>` option sets a “minimum position reliability” threshold for including positions in JSON output; the help notes that the default is 1, globe‑index options typically raise this to 2, and setting it to −1 disables speed‑based filtering, with the maximum at 4.[^24]

The related `--position-persistence <n>` option controls how aggressively readsb resists outliers, documented as “Position persistence against outliers (default: 4), incremented by json‑reliable minus 1”.[^24]
Together, these options implement a heuristic filter that down‑weights or drops positions that would imply implausible jumps in speed or heading, which includes many bogus MLAT fixes as well as faulty ADS‑B.

### 5.2 Beast‑reduce distance and altitude filters

readsb also offers Beast‑reduce network filters that can be used to discard distant or excessively high targets from downstream feeds.
The options `--net-beast-reduce-filter-dist <distance in nmi>` and `--net-beast-reduce-filter-alt <pressure altitude in ft>` “remove aircraft which are further than distance from the receiver” or “above altitude” from Beast‑reduce outputs.[^25][^24]
While aimed primarily at bandwidth reduction, these filters are also used by some aggregators to limit the impact of anomalous positions on global maps and MLAT hubs.

### 5.3 FlightAware MLAT internal filtering

As noted earlier, FlightAware’s MLAT solver does not emit results when its estimated position error exceeds 4 km.[^7]
This acts as a hard quality gate on MLAT positions, preventing most truly bogus solutions from reaching downstream consumers.
The same discussion mentions that receivers with wrong locations or unstable clocks are detected via large synchronization jitter and effectively ignored, further reducing the likelihood of severe MLAT artifacts.[^7]


## 6. Community Discussion of Bogus MLAT Positions

### 6.1 Nature of bogus MLAT artifacts

Community reports characterize bogus MLAT positions in several ways:

- Sudden jumps of 2–4 miles lateral error at low altitude during touch‑and‑go circuits, attributable to marginal geometry with only four contributing sites and one mis‑located station or unstable clock.[^13]
- Occasional far‑off tracks (hundreds of miles) believed to be software artifacts, timing glitches, or mis‑associated tracks rather than real propagation, especially when they appear only for a few messages.[^27]
- “Bogus altitudes” seen in tar1090 history when aggressive preamble thresholds are used; tuning these thresholds reduced obvious bad altitudes from roughly “2 per 100 aircraft” to “2 in 1418 aircraft” over six hours.[^28]

These observations underscore that most MLAT anomalies manifest as discrete outliers in an otherwise consistent track, rather than persistent, plausible‑looking but distant traffic.

### 6.2 Community mitigation strategies

Forum and GitHub discussions suggest the following practical mitigations for home users:

- **Rely on decoder‑level filters:** Use readsb’s `--json-reliable` and `--position-persistence` defaults or slightly stricter settings to suppress positions that would imply unrealistic speeds or track changes.[^24]
- **Use reasonable `--max-range`:** Keep `--max-range` near the physical limit (300 nm default; many community examples use 360 nm) so that grossly distant positions are dropped during CPR decoding.[^2][^23][^22]
- **Avoid feeding MLAT back into decoders:** FlightAware explicitly prohibits feeding MLAT results back as input ADS‑B data, both to protect data quality and licensing; this practice also avoids feedback loops of bogus MLAT positions.[^29]
- **Improve receiver timing and location:** For MLAT participants, ensure accurate lat/lon/alt configuration and stable NTP‑synchronized clocks; FlightAware notes that receivers with wrong locations or unstable clocks show large jitter and are automatically deprioritized or ignored.[^7]
- **Accept MLAT as “bonus” data:** Several FlightAware participants emphasize that MLAT is a “bonus” for Mode S‑only aircraft; occasional jumps or inaccuracies are expected and generally tolerated, rather than over‑tuned away at the cost of coverage.[^13]

While there is no universal community consensus on a hard MLAT range cutoff, the combination of decoder max‑range, JSON reliability filters, and MLAT solver error thresholds effectively confines most hobbyist MLAT tracks to the same 200–250 nm envelope as direct ADS‑B.


## 7. Statistics on Typical Home Receiver Ranges

### 7.1 Aggregator‑level statistics

Public documentation from large aggregators like FlightAware and ADSBexchange focuses on network scale and infrastructure, not detailed distribution statistics of home‑receiver ranges.[^30][^31]
No readily accessible source was found that publishes median, 95th‑percentile, or maximum range across all feeders.

However, FlightAware’s per‑feeder stats page and Planefinder’s polar plots provide, for individual stations, histograms of positions by distance and long‑term maximum‑range plots.[^10][^5]
Community discussions using these tools consistently show individual receivers with geometric maxima clustered in the 150–230 nm range, with only occasional outliers beyond that and clear evidence that terrain dominates the effective range.[^6][^10][^5]

### 7.2 Research network studies (OpenSky, etc.)

Academic work based on the OpenSky Network has modeled ADS‑B reception probability as a function of distance, receiver density, and interference, using large datasets from many receivers.[^32]
These studies focus on message reception probability and latency rather than geometric range limits, but their models implicitly show rapid degradation of reception probability beyond a few hundred kilometers, consistent with the line‑of‑sight and terrain limits discussed earlier.

### 7.3 Synthesizing a realistic range distribution

Given the absence of a published cross‑network percentile distribution, the best available evidence remains the combination of physical limits, vendor measurements, and many individual case studies:

- A well‑sited home receiver on a mast or rooftop, with a high‑gain 1090 MHz antenna and low‑loss cabling, typically sees:
  - Maximum ranges in its best sectors of ~180–230 nm.
  - Average effective ranges around 100–150 nm, reflecting the mix of altitudes and azimuth‑dependent terrain.[^3][^9][^4][^6]
- Indoor or partially obstructed installations generally see:
  - Maximum ranges in the 75–150 nm band.
  - Average ranges closer to 50–100 nm.[^9][^6]
- Exceptional reported maxima beyond 250 nm are rare and usually treated by their authors as anomalous (possible decoding artifacts or unusual propagation), not as repeatable performance benchmarks.[^27]

Because the physical ceiling is ~250 nm for most geometries and receiver placement is highly variable, a conservative working assumption for “realistic maximum range” of a competent home station is approximately 200 nm, with a long‑tail of rare hits extending up to ~230 nm and very few credible reports beyond that.


## 8. Key Takeaways

- **Direct ADS‑B range:** Line‑of‑sight physics and multiple independent measurements constrain realistic maximum direct ADS‑B range for home receivers to ≲250 nm, with most well‑sited installations observing maximums around 180–230 nm and average ranges near 100–150 nm.[^3][^4][^6][^9][^5]
- **MLAT range and quality:** MLAT uses the same RF paths and is therefore limited to broadly the same distance envelope; FlightAware’s implementation reports typical MLAT errors of a few hundred meters and suppresses any solutions with estimated error >4 km, preventing most bogus fixes from propagating.[^14][^7]
- **Decoder range filters:** dump1090 and readsb both implement a default absolute maximum range of 300 nm for position decoding via `--max-range`, with many deployments choosing values between 300 nm and 360 nm, and readsb adds JSON reliability and position‑persistence filters to further reject outliers.[^23][^2][^20][^22][^24]
- **Community handling of bogus MLAT:** Hobbyists generally treat MLAT as best‑effort; they rely on decoder‑level outlier suppression and MLAT solver quality thresholds, accept occasional jumps, and avoid feeding MLAT outputs back into decoders to prevent feedback loops.[^29][^13][^7]
- **Lack of global percentile statistics:** No public network‑wide statistics (median, 95th percentile, etc.) for home‑receiver ranges were found; instead, many per‑station examples cluster tightly around the physical limits implied by line‑of‑sight and terrain, suggesting that these limits are more informative than a network‑wide percentile figure.[^4][^10][^5]

---

## References

1. [What is the Maximum Range I can Get? - ADS-B Flight Tracking](https://discussions.flightaware.com/t/what-is-the-maximum-range-i-can-get/17248/148) - What is the Maximum Range I can Get? FlightAware ADS-B Flight Tracking · mgunther January 24, 2016, ...

2. [How to diagnose readsb failure - ADS-B Flight Tracking](https://discussions.flightaware.com/t/how-to-diagnose-readsb-failure/97882) - I have 2 feeders that are failing - cs0 is newly built and cs8 has been feeding since last year. Bot...

3. [Review: FlightAware 1090 MHz ADS-B Antenna and Filter](https://www.rtl-sdr.com/review-flightaware-ads-b-antenna-and-filter/) - In this post we will review the FlightAware ADS-B Antenna and their 1090 MHz band pass filter. The F...

4. [1090MHz antennae](https://arrrr.com/worter/antenna)

5. [Southern Surveillance](https://www.icao.int/APAC/Meetings/2014%20ADSBSITF13/IP22_NZ%20AI.5%20-%20Multilateration%20ADS-B%20Implementation.pdf)

6. [How can you tell if you need a better antenna? - ADS-B Flight Tracking](https://discussions.flightaware.com/t/how-can-you-tell-if-you-need-a-better-antenna/15606) - FlightAware Discussions · How can you tell if you need a better ... Realistic range of antenna · ADS...

7. [Feeding ADSBX with homebuilt antenna and RTL-SDR](https://www.reddit.com/r/ADSB/comments/1ltiomr/feeding_adsbx_with_homebuilt_antenna_and_rtlsdr/) - Feeding ADSBX with homebuilt antenna and RTL-SDR

8. [ADS-B FlightAware 1090MHz Data Antenna - 66cm / 26in for S and ADS-B modes](https://www.hamradioshop.it/gb/ads-b-flightaware-1090mhz-data-antenna-66cm-26in-for-s-and-ads-b-modes-p-9598.html) - High performance, omni directional antenna for receiving 1090MHz Mode S and ADS-B data from aircraft...

9. [Furthest distance you have picked up airports/airplanes?](https://forums.radioreference.com/threads/furthest-distance-you-have-picked-up-airports-airplanes.462734/) - Hello, I was wondering what is the furthest distance you guys n gals have picked up airport and/or a...

10. [Range plot on profile page: Nautical Miles or Statute Miles?](https://discussions.flightaware.com/t/range-plot-on-profile-page-nautical-miles-or-statute-miles/17889) - Yes I have planefinder and my maximum range for 12th Jan is 212nm. ... Flightaware - Positions repor...

11. [What ADS-B setup do u using tell us](https://www.reddit.com/r/RTLSDR/comments/1lvnd9u/what_adsb_setup_do_u_using_tell_us/) - What ADS-B setup do u using tell us

12. [ADS-B Questions](https://www.reddit.com/r/RTLSDR/comments/15mrq41/adsb_questions/) - ADS-B Questions

13. [What is the Maximum Range I can Get? - ADS-B Flight Tracking](https://discussions.flightaware.com/t/what-is-the-maximum-range-i-can-get/17248?page=5) - What is the Maximum Range I can Get? · The heatmap generates the grid centered on the SiteLat and Si...

14. [MLAT distance - ADS-B Flight Tracking](https://discussions.flightaware.com/t/mlat-distance/18056) - Hello All, I know for MLAT the great the distance between RX stations the better …BUT… what would th...

15. [[PDF] A k-NN-based Localization Approach for Crowdsourced Air Traffic ...](https://www.cs.ox.ac.uk/files/9692/A%20k-NN-based%20Localization%20Approach%20for%20Crowdsourced%20Air%20Traffic%20Communication%20Networks.pdf) - The computational time is the trade-off for k-NN's accuracy and robustness. Only with the largest sq...

16. [Multilateration system（MLAT）｜JRC（Japan Radio Co.,Ltd.）www.jrc.co.jp › product › mlatj](https://www.jrc.co.jp/en/product/mlatj) - It is on the page of 「Multilateration system（MLAT）」.Japan Radio Co., Ltd. is a professional group of...

17. [Multilateration System](https://www.jrc.co.jp/hubfs/jrc-corp/assets/pdf/product/mlat_e.pdf)

18. [mlat-client/README.md at master · mutability/mlat-client](https://github.com/mutability/mlat-client/blob/master/README.md) - Mode S multilateration client. Contribute to mutability/mlat-client development by creating an accou...

19. [GitHub - mutability/mlat-client: Mode S multilateration client](https://github.com/mutability/mlat-client) - Mode S multilateration client. Contribute to mutability/mlat-client development by creating an accou...

20. [MLAT Network](https://discussions.flightaware.com/t/mlat-network/18042) - Does the PiAware Network and the FlightFeeder Network work along to generte (calculate) MLAT? Or are...

21. [Not many MLAT for me - Page 2 - ADS-B Flight Tracking](https://discussions.flightaware.com/t/not-many-mlat-for-me/43383?page=2) - I work with an engineer whose answers seem to contradict each other, but upon careful examination, h...

22. [Own receiver position in MLAT feeder map - ADSB Exchange](https://adsbx.discourse.group/t/own-receiver-position-in-mlat-feeder-map/751) - I can not see my receiver position in the ADSB Feeder map (ADSBexchange.com Feeder Coverage). In the...

23. [Dump1090 - Message rate too high in Skyaware](https://discussions.flightaware.com/t/dump1090-message-rate-too-high-in-skyaware/77163) - Besides the bogus message rate no issue unless you're short on CPU. ... If they pull it off modifyin...

24. [What is the Maximum Range I can Get? - ADS-B Flight Tracking](https://discussions.flightaware.com/t/what-is-the-maximum-range-i-can-get/17248?page=2) - What is the Maximum Range I can Get? ; wnypoker December 21, 2015, 4:07pm 21 ; phillx19090 December ...

25. [adsbexchange distance calculation not showing on map - Facebook](https://www.facebook.com/groups/sdraustralia/posts/1147996473252238/) - Software is running, feeding ADSBExchange, syncing with peers. and my feeder shows up on the MLAT ma...

26. [globe history pruning: be a bit more verbose (#116) · 58465dd35d](https://git.c-l.it/c-l/docker-adsb-ultrafeeder/commit/58465dd35de7f741319586af8d3272750b56e4df) - * globe history pruning: be a bit more verbose also remove empty directories * README: some location...

27. [The range was *what* now?!](https://www.reddit.com/r/ADSB/comments/z1fiet/the_range_was_what_now/) - The range was *what* now?!

28. [Airspy mini and Airspy R2: Piaware / dump1090-fa configuration](https://discussions.flightaware.com/t/howto-airspy-mini-and-airspy-r2-piaware-dump1090-fa-configuration/44343?page=167) - With -P 4 and much lower preambles before today, I would see about 2 (obvious) last report bogus alt...

29. [piaware/MLAT-RESULTS-LICENSE.md at master · flightaware/piaware](https://github.com/flightaware/piaware/blob/master/MLAT-RESULTS-LICENSE.md) - Client-side package and programs for forwarding ADS-B data to FlightAware - flightaware/piaware

30. [ADS-B Exchange: Serving the Flight Tracking Enthusiast](https://www.adsbexchange.com) - Join the Community to Unlock Early Access to New Releases & More What is ADSBx? The world’s largest ...

31. [FlightAware's Terrestrial ADS-B Network - Angle of Attack](https://flightaware.engineering/flightawares-terrestrial-ads-b-network/) - An inside-look into our worldwide network of ADS-B ground stations.

32. [ModellingADS-BReceptionProbabilityUsingOpenSkyData](https://journals.open.tudelft.nl/joas/article/download/7895/6436/33718)

