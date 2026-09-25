# Try Odoo 20 and the AI Assistant in a local VM

`setup_odoo_vm.sh` turns a fresh Ubuntu 24.04 machine into a test server. It installs:

- PostgreSQL and Odoo 20 from this repository, running as a service on port 8069
- a database `odoo` with demo data, the Contacts app and the AI Assistant module
- [Ollama](https://ollama.com) with a small model (`qwen3:4b`), already connected to the AI Assistant

## 1. Create the VM

The steps below use [Multipass](https://canonical.com/multipass), which runs Ubuntu VMs on
Windows, macOS and Linux. Install it, then in a terminal:

```bash
multipass launch 24.04 --name odoo --cpus 4 --memory 8G --disk 40G
```

A local LLM is the heavy part: give the VM at least 8 GB of RAM for `qwen3:4b`, and 12 GB or
more for 8B models.

Any other Ubuntu 24.04 VM (VirtualBox, Hyper-V, UTM, Proxmox) works too: copy the script into
it and continue at step 2.

## 2. Run the setup script

```bash
multipass transfer local_vm/setup_odoo_vm.sh odoo:
multipass exec odoo -- sudo bash setup_odoo_vm.sh
```

It takes 10 to 20 minutes, mostly for Python packages and the model download. At the end it
prints the address to open, for example `http://192.168.64.5:8069`. Log in with `admin` / `admin`.

Options, set before the command, e.g. `sudo LLM_MODEL=qwen3:8b bash setup_odoo_vm.sh`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `LLM_MODEL` | `qwen3:4b` | Ollama model to download and use (it must support tool calling) |
| `REPO_URL` | this fork on GitHub | Git repository to run |
| `REPO_BRANCH` | `20.0` | Branch to check out |
| `SKIP_OLLAMA` | `0` | `1` to skip Ollama, e.g. to use an LLM server elsewhere |

Running the script again is safe: it updates the code and the module, and keeps the database.

## 3. Try the assistant

Open *AI Assistant > Chat* and ask, for example:

- "Which companies are in the United States?"
- "How many contacts do we have per country?"
- "Change the phone of Azure Interior to +1 870 555 0100"

The last one creates a proposal: open the link in the answer and click *Confirm*. Nothing
changes before that. Every answer and its tool calls are logged in *AI Assistant >
Configuration > Answers*, which helps to see what the model did.

On a CPU, a small model needs from a few seconds to a minute per step, and a question can take
several steps. Small models also make more mistakes with tools than big ones: if the answers
are poor, try a bigger model (`sudo ollama pull qwen3:8b`, then change the model in *Settings >
AI Assistant*).

## Everyday commands

```bash
multipass shell odoo                      # open a shell in the VM
sudo systemctl restart odoo               # restart Odoo
sudo tail -f /var/log/odoo/odoo.log       # follow the Odoo log
sudo -u odoo git -C /opt/odoo/src pull && sudo systemctl restart odoo   # get new code
multipass stop odoo / multipass start odoo
multipass delete --purge odoo             # remove the VM
```

After changing the module's Python or XML files, update it with
`sudo -u odoo /opt/odoo/venv/bin/python /opt/odoo/src/odoo-bin -c /etc/odoo/odoo.conf -u llm_assistant --stop-after-init`
and restart Odoo.

This setup is for trying things out: it uses the default `admin` password and plain HTTP.
Don't expose it to the internet.
