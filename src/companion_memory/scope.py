"""Shared scene selector. Event origin and applicability are separate fields."""

from .contracts import Scene


def scene_matches(applies_to: Scene, scene: Scene) -> bool:
    return all(
        not required or required == actual
        for required, actual in (
            (applies_to.plugin_id, scene.plugin_id),
            (applies_to.game_id, scene.game_id),
            (applies_to.session_id, scene.session_id),
        )
    )

