# Restart FRITZ!Box with Shelly

## Goal

Automatically recover the home internet connection when the Vodafone Cable connection gets stuck.

Observed behavior:

- The computer can still reach the FRITZ!Box over LAN/Wi‑Fi.
- The internet connection is sometimes completely unavailable.
- A physical power cycle of the FRITZ!Box fixes the problem.
- Because the internet is down during the failure, remote access through Tailscale cannot be relied on.
- Therefore the recovery must happen locally and automatically.

---

## Setup

```text
Internet / Vodafone Cable
          |
      FRITZ!Box
          |
   Home LAN / Wi‑Fi
          |
     Watchdog PC
          |
      Shelly Plug
          |
 FRITZ!Box power supply
```

The FRITZ!Box power supply is plugged into the Shelly.

The watchdog computer stays on and periodically checks whether the internet is reachable.

If the internet stays unavailable long enough, the watchdog tells the Shelly to cut power to the FRITZ!Box briefly and then restore it automatically.

---

## Important: Give the Shelly a Fixed IP

Assign the Shelly a fixed/local IP so the watchdog can always reach it.

Example:

```text
192.168.178.50
```

Use either:

- a fixed IP configured on the Shelly, or
- a permanent DHCP assignment in the FRITZ!Box.

The exact IP does not matter, but it must stay predictable.

---

## Shelly Power-Cycle Command

For Shelly devices using the modern RPC API, the idea is:

```bash
curl -X POST   -H "Content-Type: application/json"   -d '{"id":1,"method":"Switch.Set","params":{"id":0,"on":false,"toggle_after":5}}'   http://192.168.178.50/rpc
```

Replace:

```text
192.168.178.50
```

with the actual Shelly IP.

### What this does

```json
"on": false,
"toggle_after": 5
```

means:

1. Switch the Shelly output OFF immediately.
2. Wait 5 seconds.
3. Toggle the output back ON automatically.

So the FRITZ!Box does this:

```text
FRITZ!Box power OFF
        |
     5 seconds
        |
FRITZ!Box power ON
```

The important part is that the delayed ON action is handled by the Shelly itself.

The watchdog only has to successfully send the initial command.

Once the FRITZ!Box powers off, the home network can disappear temporarily and the Shelly will still restore power after the timer expires.

> Note: Exact Shelly API commands depend on the Shelly generation/model. The command above is for Shelly models using the RPC API. Verify the exact model before deployment.

---

## Watchdog Logic

Do not reboot the FRITZ!Box immediately after one failed ping.

A short Vodafone interruption may recover by itself.

Recommended logic:

```text
Check internet every 30 seconds
        |
        v
Internet works?
   |        |
  YES      NO
   |        |
nothing   wait 60 seconds
            |
            v
       check again
            |
       internet works?
          |      |
         YES     NO
          |       |
       nothing   verify outage
                  |
                  v
       test multiple destinations
                  |
                  v
          still offline?
             |       |
            NO      YES
             |       |
          nothing   power-cycle
                     FRITZ!Box
                        |
                        v
                 wait for boot
                        |
                        v
                  test internet
```

---

## Internet Checks

Use more than one target so that one unreachable service does not cause an unnecessary reboot.

For example:

```text
1.1.1.1
8.8.8.8
https://example.com
```

A good rule:

```text
Only consider the internet down if all checks fail.
```

It can also be useful to check that the FRITZ!Box itself is reachable:

```text
fritz.box
192.168.178.1
```

Ideal failure condition:

```text
FRITZ!Box reachable
AND
multiple internet checks fail
```

This strongly suggests:

```text
local network = working
internet/WAN   = broken
```

---

## Retry Strategy

Do not power-cycle continuously during a real Vodafone area outage.

Recommended retry timing:

```text
Initial failure:
    wait 60 seconds before doing anything

Power cycle #1:
    immediately after confirmed sustained outage

If still offline:
    retry after 10 minutes

If still offline:
    retry after 15 minutes

Further retries:
    every 30 minutes
```

This handles three common cases.

### Short interruption

```text
Vodafone blip
    |
internet disappears briefly
    |
internet returns by itself
    |
NO reboot
```

### FRITZ!Box / cable modem stuck

```text
internet disappears
    |
does not recover
    |
watchdog power-cycles FRITZ!Box
    |
internet returns
```

### Real Vodafone outage

```text
internet disappears
    |
reboot does not fix it
    |
watchdog waits
    |
tries again occasionally
```

This avoids rebooting the FRITZ!Box every few minutes for hours.

---

## Boot Waiting Time

After a power cycle, do not immediately decide that the reset failed.

Cable synchronization can take several minutes.

Recommended initial wait:

```text
5 minutes
```

Then begin internet checks again.

If the FRITZ!Box normally takes longer to reconnect on this Vodafone connection, increase this value.

---

## Logging

The watchdog should log every relevant event.

Example:

```text
2026-09-17 03:14:02 Internet check failed
2026-09-17 03:15:02 Internet still unavailable
2026-09-17 03:15:03 FRITZ!Box reachable locally
2026-09-17 03:15:03 Starting power cycle #1
2026-09-17 03:15:08 Shelly restored FRITZ!Box power
2026-09-17 03:20:08 Internet reachable again
2026-09-17 03:20:08 Total outage: 6m 06s
```

Useful fields:

```text
timestamp
internet status
FRITZ!Box local status
power-cycle count
time internet returned
total downtime
```

Over time this will show:

- how often the connection fails,
- whether the failures happen at particular times,
- how often a FRITZ!Box reboot fixes them,
- how long recovery normally takes,
- whether Vodafone is experiencing longer external outages.

---

## Tailscale

Tailscale can still be useful for normal remote access to the home network.

However:

```text
Vodafone internet DOWN
        |
        v
home Tailscale node cannot reach Tailscale network
        |
        v
remote Shelly control unavailable
```

Therefore Tailscale is not the primary recovery mechanism.

The watchdog must run locally.

Once the watchdog restores the Vodafone connection, Tailscale should reconnect automatically.

---

## Recommended Watchdog Machine

The watchdog can run on any device that normally stays powered on:

- Windows PC
- Linux server
- Raspberry Pi
- NAS
- mini PC

It must be connected to the same local network as the Shelly.

The watchdog does **not** need the FRITZ!Box administrator password for the Shelly-based recovery method.

### BrandMonitor server

For this setup, the watchdog will run on the **BrandMonitor server**, which is the Dell laptop.

Before the watchdog performs any outage-recovery action, it must check the BrandMonitor `run.lock` state.

Required behavior:

```text
Check run.lock
     |
     +-- explicitly false --> watchdog may continue
     |
     +-- true -------------> do nothing
     |
     +-- missing ----------> do nothing
     |
     +-- unreadable -------> do nothing
```

The watchdog should only proceed when `run.lock` explicitly indicates:

```text
false
```

This is a fail-safe rule. A false-positive internet check must never interrupt an active BrandMonitor run.

So the recovery flow becomes:

```text
Internet appears down
        |
        v
wait / verify outage
        |
        v
check run.lock
        |
   +----+----+
   |         |
 false      anything else
   |         |
   v         v
continue    skip recovery
   |
   v
confirm FRITZ!Box reachable locally
   |
   v
confirm multiple internet targets fail
   |
   v
Shelly power-cycle allowed
```

The `run.lock` check should happen again immediately before sending the Shelly power-cycle command, in case a BrandMonitor run started while the outage checks were in progress.

Recommended rule in pseudocode:

```text
if run.lock is not explicitly false:
    exit without resetting anything

verify internet outage

if run.lock is not explicitly false:
    exit without resetting anything

power-cycle FRITZ!Box
```

---

## Safety Checks

Before enabling automatic resets:

1. Confirm that the Shelly controls only the FRITZ!Box power supply.
2. Confirm that `OFF + toggle_after` reliably turns power back on.
3. Test the command manually while physically at home.
4. Confirm that the Shelly reconnects to Wi‑Fi after the FRITZ!Box boots.
5. Confirm that the watchdog can still reach the Shelly after a normal reboot.
6. Use a retry delay so a long Vodafone outage does not cause endless rapid power cycles.

---

## Manual Test

While at home, run:

```bash
curl -X POST   -H "Content-Type: application/json"   -d '{"id":1,"method":"Switch.Set","params":{"id":0,"on":false,"toggle_after":5}}'   http://192.168.178.50/rpc
```

Expected result:

```text
Shelly OFF
    |
FRITZ!Box loses power
    |
5 seconds
    |
Shelly ON
    |
FRITZ!Box boots
    |
Vodafone Cable reconnects
    |
internet returns
```

Only after this works reliably should the automatic watchdog be enabled.

---

## Final Intended Behavior

```text
Internet healthy
     |
watchdog checks periodically
     |
internet fails
     |
wait for transient outage to recover
     |
still failed
     |
confirm FRITZ!Box is locally reachable
     |
confirm multiple internet targets fail
     |
Shelly power-cycles FRITZ!Box
     |
wait for cable synchronization
     |
internet restored?
   |          |
  YES        NO
   |          |
 log OK    wait/retry
```

The key design principle is:

> Recovery happens entirely inside the home network and does not depend on the internet connection that is being repaired.

Additionally, the BrandMonitor `run.lock` guard always takes priority over recovery. The FRITZ!Box must not be power-cycled unless `run.lock` is explicitly `false`.
