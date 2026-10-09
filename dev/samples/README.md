# Parser samples

Used by `dev/regress.py` (R21, R23). **Only the `snmp_*.txt` files are real**:
captured from net-snmp's `snmpget`/`snmpwalk -Oqn` against a real `snmpd` in an
Alpine container.

The `eos_*` and `junos_*` files are **hand-written** from the vendors'
documented output formats. They prove the parsers do what they were written to
do, not that the parsers match real devices. When you have real EOS or Junos
output, replace the sample and keep the expectations in `regress.py` honest.
