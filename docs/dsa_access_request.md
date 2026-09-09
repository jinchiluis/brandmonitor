# DSA Transparency Database — Research API access (granted)

## Is commercial use allowed? Yes — checked 2026-09-09

Two different DSA mechanisms get confused, including by search engines. They are
not the same thing and only one of them is restrictive:

| | **Article 40 vetted researcher access** | **Transparency Database Research API** |
|---|---|---|
| What data | Platforms' **internal** data | **Public** statements of reasons |
| Who | Vetted by a national Digital Services Coordinator | Anyone with an EU Login account |
| Requires | Research institution affiliation, **independence from commercial interests**, results published free | An e-mail to the helpdesk |
| Relevant to us | **No** | **Yes** |

The "independence from commercial interests" condition belongs to Article 40 and
does **not** apply here. The Transparency Database is publicly accessible — anyone
can search, read and download statements of reasons — and the Research API is a
convenience layer over that same public data. Its documentation states no academic
affiliation, institutional, credential or non-commercial requirement of any kind; it
addresses "interested stakeholders with the relevant technical knowledge".

**The data is licensed CC BY 4.0**, which permits commercial reuse with attribution.
So selling client reports built on it is fine, provided the reports carry the
credit. Required attribution string:

> European Commission-DG CONNECT, "Digital Services Act Transparency Database",
> Directorate-General for Communications Networks, Content and Technology, 2023

Note also: changes must be indicated, industrial property rights (logos,
trademarks, names) are excluded from the reuse permission, and additional rights
clearance may be needed where content involves identifiable private individuals.

**Conclusion: do not dress this up as academic research.** Access is open, the
licence permits commercial use, and claiming a research posture you do not have
would be both unnecessary and a risk to the token later. A plain request is
correct — and a shorter one than an over-justified one, because there is no
eligibility case to argue.

## Before sending

1. Create an EU Login account if you do not have one: <https://webgate.ec.europa.eu/cas/>
2. Send the mail below to **CNECT-DSA-HELPDESK@ec.europa.eu**, from the address
   tied to that account.
3. They add the permission; you then generate the token from the Transparency
   Database site while logged in.

---

**Subject:** Request for Research API authentication token — DSA Transparency Database

Dear DSA Helpdesk,

I would like to request an authentication token for the Research API of the DSA
Transparency Database.

- Name: [your full name]
- Organisation: [Vivolution — legal entity name]
- EU Login account (e-mail): [the address registered with EU Login]
- Website: [company URL]

We operate a media and regulatory monitoring service for companies active in
European e-commerce and parcel logistics. We would use the API to follow publicly
available statement-of-reasons data for designated VLOPs — principally aggregate
volumes and category distributions over time — as an input to regulatory monitoring
reports prepared for our clients.

Our expected usage is modest: periodic `/count` and `/aggregates` queries to track
daily volumes per platform and category, and occasional narrow `/search` queries
limited to a single platform, category and date. We do not intend to bulk-extract
the database, and we understand the documented limits (1,000 rows per query, no
pagination, 5 MB response, 30-second execution). We will keep the token
confidential and use it within those limits.

Please let me know if you need any further information.

Kind regards,
[name]
[position, organisation]
[phone / address]

---

## Granted 2026-09-09

The token is in `.env` as **`DSA_KEY`** (never committed — see CLAUDE.md, "Hosts
and secrets"). The request above worked as written; no follow-up questions were
asked. This file is kept as the record of why a plain, non-academic request was
the correct posture, in case the token ever has to be re-requested.

The collector is `src/dsa.py` (`python run.py collect-dsa`). Measured endpoint
behaviour — including three previously recorded constraints that turned out to be
wrong — is in [source_coverage.md](source_coverage.md).

The CC BY 4.0 attribution has to appear in any client deliverable that uses this
data. It is stored in every collected payload so it travels with the numbers.
