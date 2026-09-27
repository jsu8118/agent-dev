# Mechanical Seal Failure Analysis Guide (MS-100 / MS-250 / MS-400)

Document SEAL-FA-02, revision A (2025-06). Used by field service and quality engineering when inspecting returned seals and pumps.

## 1. How a mechanical seal works

A rotating seal face (carbon) runs against a stationary face (silicon carbide). A liquid film of about one micrometer between the faces lubricates and cools them. Without liquid (dry running) the faces overheat within seconds.

## 2. Reading a failed seal

| Evidence on the seal | Most likely cause | Warranty relevance (WAR-001) |
|---|---|---|
| Radial heat cracks ("heat checking") on the silicon-carbide face; charred or hardened elastomers | **Dry running** | Excluded (§3.1) |
| Chipped face edges, deep grooves, embedded particles | Abrasive solids in the liquid | Excluded if the liquid was outside the specification; otherwise investigate |
| Swollen, soft, or dissolved O-rings / bellows | **Elastomer incompatible with the liquid** | Excluded if the customer specified the liquid incorrectly; covered if Kestrel selected the wrong elastomer |
| Uneven wear track on the carbon face | Misalignment or shaft deflection | Covered only if installation by Kestrel |
| **Cracked or deformed O-ring groove in the gland; face flatness out of tolerance on a new seal (< 300 hours)** | **Manufacturing defect** | **Covered** |
| Leakage from the start, no face damage, wrong setting dimension | Incorrect assembly | Covered if assembled by Kestrel (factory or SVC-INSTALL) |

## 3. Factory quality checks

* Every MS-250 face pair is lapped to a flatness of **2 helium light bands** or better.
* Plant P2 (Lakeside) performs a **hydrostatic test at 1.5 × maximum working pressure for 10 minutes** on every assembled pump. A seal "weep" during the test is logged as a test failure and the seal is replaced.
* Incoming seal kits are sampled per lot (AQL 1.0). A lot with two or more nonconformities in the sample is placed on **quality hold**.

## 4. When several failures share a cause

If failures on different pumps, sites, or customers show the **same evidence** (for example, O-ring groove cracks) and the seals come from the **same supplier lot**, treat it as a potential **systemic defect**: place remaining stock of that lot on hold, notify Quality Engineering, and check the RMA history for field failures of pumps built with that lot.
