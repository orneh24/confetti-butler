# Parser samples

Used by `dev/regress.py` (R23, R30).

- `snmp_*.txt` are **real**: captured from net-snmp's `snmpget` / `snmpwalk -Oqn` against a real `snmpd` in an
  Alpine container.
- `ios_bgp_summary_15_4.txt` has the **real format** of `show bgp summary` from an IOS-XE 15.4 router (an
  administratively shut neighbor prints as `Idle (Admin)`); the addresses were replaced with documentation
  ranges.
