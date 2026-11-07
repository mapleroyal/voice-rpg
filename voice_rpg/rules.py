"""The executable rules profile; constants and mechanical arithmetic live here."""

import copy
import random

RULES = {
    "id": "story-2d6", "version": "2",
    "dice": {"count": 2, "sides": 6},
    "talents": {"initial_count": 3, "initial_rating": 1, "maximum": 2},
    "health": {"maximum": 6, "restorative": 2,
               "harm": {"minor": 1, "moderate": 2, "severe": 3},
               "rest_requirement": "Safe, uninterrupted rest.",
               "at_zero": "Incapacitated: no ordinary physical exertion. Resolve defeat according to established stakes, including death when lethal danger was clear. Zero health alone does not decide the outcome; speech, help and recovery depend on the saved circumstances."},
    "difficulties": {"easy": 1, "standard": 0, "hard": -1},
    "default_difficulty": "standard",
    "starting_gear_quantity": 1, "starting_restorative_quantity": 2,
    "tiers": [
        {"name": "setback", "maximum": 6,
         "meaning": "Deny the stated aim and change the situation through a meaningful consequence."},
        {"name": "mixed", "maximum": 9,
         "meaning": "Achieve the aim with one proportionate cost, or achieve a clearly described reduced version."},
        {"name": "success", "maximum": None,
         "meaning": "Achieve the stated aim."},
    ],
}


def _error(message, details=None):
    from .contracts import GameError
    raise GameError("invalid_rule", message, details)


def _integer(value, name, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        _error("Invalid " + name + ".")
    return value


def _health(health):
    return _integer(health, "health", 0, RULES["health"]["maximum"])


def tier_for(total):
    if type(total) is not int:
        _error("A check total must be an integer.")
    for tier in RULES["tiers"]:
        if tier["maximum"] is None or total <= tier["maximum"]:
            return tier["name"]


def resolve_check(talent=None, difficulty="standard", character=None, dice=None, rng=None):
    """Roll once, or recompute a corrected check using explicitly supplied raw dice."""
    if type(character) is not dict or type(character.get("talents")) is not dict:
        _error("A check requires the saved player character.")
    if type(difficulty) is not str or difficulty not in RULES["difficulties"]:
        _error("Unknown difficulty.", {"known_difficulties": list(RULES["difficulties"])})
    if talent is not None and (type(talent) is not str or talent not in character["talents"]):
        _error("Unknown talent.", {"known_talents": list(character["talents"])})
    modifier = 0 if talent is None else character["talents"][talent]
    _integer(modifier, "talent rating", 0 if talent is None else 1, RULES["talents"]["maximum"])
    if dice is None:
        generator = rng if rng is not None else random.SystemRandom()
        dice = [generator.randint(1, RULES["dice"]["sides"]) for _ in range(RULES["dice"]["count"])]
    if type(dice) is not list or len(dice) != RULES["dice"]["count"]:
        _error("A check requires exactly the profile's number of raw dice.")
    for die in dice:
        _integer(die, "die", 1, RULES["dice"]["sides"])
    difficulty_modifier = RULES["difficulties"][difficulty]
    total = sum(dice) + modifier + difficulty_modifier
    return {"talent": talent, "modifier": modifier, "difficulty": difficulty,
            "difficulty_modifier": difficulty_modifier, "dice": list(dice),
            "total": total, "tier": tier_for(total)}


def health_label(health):
    _health(health)
    if health == RULES["health"]["maximum"]:
        return "well"
    if health >= 3:
        return "hurt"
    if health >= 1:
        return "badly hurt"
    return "incapacitated"


def apply_harm(health, severity):
    _health(health)
    if type(severity) is not str or severity not in RULES["health"]["harm"]:
        _error("Unknown harm severity.")
    return max(0, health - RULES["health"]["harm"][severity])


def heal(health):
    return min(RULES["health"]["maximum"], _health(health) + RULES["health"]["restorative"])


def rest():
    return RULES["health"]["maximum"]


def consume_stock(quantity, amount):
    _integer(quantity, "stock")
    _integer(amount, "consumed quantity", 1)
    if amount > quantity:
        _error("Insufficient item stock.", {"available": quantity, "requested": amount})
    return quantity - amount


def advance_talent(talents, talent):
    if type(talents) is not dict or type(talent) is not str or not talent.strip() or len(talent) > 80:
        _error("Invalid talent selection.")
    result = copy.deepcopy(talents)
    for name, rating in result.items():
        if type(name) is not str or not name.strip() or len(name) > 80:
            _error("Invalid existing talent name.")
        _integer(rating, "talent rating", 1, RULES["talents"]["maximum"])
    if talent in result:
        if result[talent] >= RULES["talents"]["maximum"]:
            _error("This talent is already at its maximum rating.")
        result[talent] += 1
    else:
        if talent.casefold().strip() in {name.casefold().strip() for name in result}:
            _error("A differently capitalized or padded version of this talent already exists.")
        result[talent] = RULES["talents"]["initial_rating"]
    return result


def rules_capsule():
    """A fresh serializable view, generated from the executable profile."""
    capsule = copy.deepcopy(RULES)
    capsule["health"]["labels"] = {str(value): health_label(value)
                                    for value in range(RULES["health"]["maximum"] + 1)}
    return capsule
