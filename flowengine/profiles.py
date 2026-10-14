"""Compatibility profiles: explicit, versioned sets of semantic switches.

A profile names a reference product build + engine mode. Kernels consult
``ctx.profile`` only for behaviours where the reference engines are *observed*
to differ; every non-default switch value cites its differential evidence.
Profiles never change behaviour silently: the active profile is recorded in
every run manifest.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from .errors import ConfigError


@dataclass(frozen=True)
class Profile:
    name: str
    product_build: str
    engine: str  # 'e1' | 'e2'
    status: str  # 'evidence-backed' | 'inherited-unverified'
    # --- semantic switches (defaults = E1 observations, build 2026.2.0.8) ---
    grouped_output_order: str = 'key'          # 'key' | 'unordered-stable' — Summarize/CrossTab/RunningTotal/Unique/grouped Sample
    join_output_order: str = 'key'             # 'key' | 'unspecified'
    double_text: str = 'g15'                   # 'g15' (15 significant digits) | 'shortest' (round-trip repr)
    # catalogue ids whose output row order the reference does not define under this profile
    order_unspecified: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {'name': self.name, 'product_build': self.product_build, 'engine': self.engine, 'status': self.status,
                'grouped_output_order': self.grouped_output_order, 'join_output_order': self.join_output_order,
                'double_text': self.double_text, 'order_unspecified': list(self.order_unspecified),
                'notes': list(self.notes), 'evidence': dict(self.evidence)}


_E1 = Profile('ayx-2026.2-e1', '2026.2.0.8', 'e1', 'evidence-backed',
              notes=('Primary reference profile. All [ref:*] rules in docs/ENGINE_SEMANTICS.md were observed here.',),
              evidence={'results': 'conformance/results/2026.2.0.8/e1/results.json'})

PROFILES: dict[str, Profile] = {
    _E1.name: _E1,
    # E2 values are filled from conformance/results/2026.2.0.8/e2 (see docs/COMPATIBILITY_PROFILES.md)
    'ayx-2026.2-e2': replace(_E1, name='ayx-2026.2-e2', engine='e2',
                             grouped_output_order='unspecified', join_output_order='unspecified',
                             double_text='shortest',
                             order_unspecified=('join:join', 'transform:summarize', 'transform:cross-tab',
                                                'transform:running-total', 'preparation:sample[grouped]',
                                                'preparation:imputation'),
                             notes=('AMP engine. Switches differ from E1 only where E2 differential cases show it: '
                                    'doubles render shortest round-trip [ref:e2/formula-rounding]; group and join output '
                                    'order follows no stable rule [ref:e2/summarize-basic, e2/join-many-to-many-nulls, '
                                    'e2/probe-join-unjoined-order, e2/sample-first-grouped, e2/imputation-mean-macro]. flowengine still emits key order (a valid instance '
                                    'of an unspecified order) so its own runs stay deterministic.',),
                             evidence={'results': 'conformance/results/2026.2.0.8/e2/results.json'}),
    'ayx-2026.1-e1': replace(_E1, name='ayx-2026.1-e1', product_build='2026.1.4.126', status='inherited-unverified',
                             notes=('No 2026.1 install available; inherits 2026.2 E1 rules. Public 2026.1 and current '
                                    'tool lists were identical on 2026-10-13.',), evidence={}),
}
DEFAULT_PROFILE = _E1.name


def get_profile(name: str | None) -> Profile:
    if name is None:
        return PROFILES[DEFAULT_PROFILE]
    if name not in PROFILES:
        raise ConfigError(f'Unknown compatibility profile {name!r}; available: {sorted(PROFILES)}')
    return PROFILES[name]
