# Minimal stand-in for a remote box fwd can be pointed at, so the ssh/sync/bootstrap/tmux layers get exercised
# end-to-end with no cloud spend and no dependency on the user's own machines.
#
# Deliberately close to the worst realistic case: an unprivileged user with no sudo, so anything the bootstrap does
# must work user-space. curl/unzip are present because that is what the real installers need; tmux and rsync are
# preinstalled because fwd expects to find (or apt-install) them rather than build them. node/npm stand in for a
# machine that already has a JS runtime, which lets the shared resolver install pnpm persistently when that scenario
# declares it.
FROM ubuntu:24.04

ARG PUBKEY

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        openssh-server rsync tmux curl unzip ca-certificates git bash \
        nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged, sudo-less user. ubuntu:24.04 already ships a uid-1000 "ubuntu" account, so dev lands on 1001.
# `usermod -p '*'` is load-bearing: useradd leaves the password field as "!" (locked), and with UsePAM no sshd rejects
# a locked account outright — "User dev not allowed because account is locked" — even for a valid public key.
# "*" means "no password will ever match" without marking the account locked, which is what key-only auth needs.
RUN useradd -m -s /bin/bash dev \
    && usermod -p '*' dev \
    && mkdir -p /home/dev/.ssh \
    && printf '%s\n' "$PUBKEY" > /home/dev/.ssh/authorized_keys \
    && chmod 700 /home/dev/.ssh \
    && chmod 600 /home/dev/.ssh/authorized_keys \
    && chown -R dev:dev /home/dev/.ssh

# Key-only auth, matching how fwd connects (BatchMode means a password prompt would hang rather than fail).
RUN mkdir -p /run/sshd \
    && ssh-keygen -A \
    && printf '%s\n' \
        'PermitRootLogin no' \
        'PasswordAuthentication no' \
        'PubkeyAuthentication yes' \
        'UsePAM no' \
        'AcceptEnv LANG LC_*' \
        > /etc/ssh/sshd_config.d/fwd-test.conf

EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
