from ..contracts import ContextRequest, PersonaProfile
from ..scope import scene_matches


def render_profile(profile: PersonaProfile, request: ContextRequest) -> str:
    if profile.character != request.scope.character:
        raise ValueError("profile character differs from requested character")
    return "\n".join(
        section.text
        for section in profile.sections
        if section.subject in (None, request.scope.subject)
        and scene_matches(section.applies_to, request.scene)
    )

