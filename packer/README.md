# Packer template (vCenter)

Builds the confetti-butler golden template without the manual steps: Alpine
install, upload of `butler/`, `build-template.sh`, shutdown, convert to template.

**Status: not run.** There was no vCenter to try it on, so treat it as a
starting point. The VM-side result of the build (what `build-template.sh`
produces) *has* been verified on a real Alpine 3.24.2 VM; what is untested is
the Packer part that drives vCenter and types into the console.

## Use

1. Upload an Alpine **virt** ISO to a datastore.
2. `cp packer/vars.pkrvars.hcl.example packer/vars.pkrvars.hcl` and fill it in
   (the real file is git-ignored; it holds passwords).
3. From the repo root:
   `packer init packer/` then `packer build -var-file=packer/vars.pkrvars.hcl packer/`

## What will probably need adjusting

- **`boot_command` timings.** `<wait>` values assume a fast datastore. If the
  installer is not at the expected prompt when a line is typed, raise the wait.
- **Device names.** The answer file assumes `eth0` and `/dev/sda` (a `pvscsi`
  disk). A different adapter or controller changes those.
- **Prompts.** `setup-alpine` asks for the root password twice and for a "y" before
  erasing the disk; the sequence is in `boot_command`. A different Alpine
  release may add or reorder prompts.
- **After the build** the template has no SSH host keys and an empty database;
  first boot of a clone regenerates keys (see `butler/build-template.sh`).
