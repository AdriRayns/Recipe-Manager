"""Sensor platform exposing each recipe as a state-machine entity.

This lets fully native dashboards (picture-entity, markdown+Jinja,
auto-entities, etc.) browse and render recipes without the custom card.
The card remains the only way to edit; entities are read-only mirrors
kept in sync with the storage via the recipe_manager_recipe_* bus events.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    EVENT_RECIPE_ADDED,
    EVENT_RECIPE_DELETED,
    EVENT_RECIPE_UPDATED,
)
from .models import Recipe
from .storage import RecipeStorage

_LOGGER = logging.getLogger(__name__)

DATA_STORAGE = f"{DOMAIN}_storage"
DATA_SENSOR_MANAGER = f"{DOMAIN}_sensor_manager"

# HA caps state values at 255 characters
MAX_STATE_LEN = 255


def _ingredient_to_dict(ing) -> Dict:
    d = {"name": ing.name}
    if ing.amount:
        d["amount"] = ing.amount
    if ing.unit:
        d["unit"] = ing.unit
    if ing.notes:
        d["notes"] = ing.notes
    if ing.is_heading:
        d["is_heading"] = True
    return d


def _recipe_attributes(recipe: Recipe) -> Dict:
    """Flat attribute map for a recipe (JSON-serialisable)."""
    attrs: Dict = {
        "recipe_id": recipe.id,
        "description": recipe.description or "",
        "image_url": recipe.image_url or "",
        "source_url": recipe.source_url or "",
        "cuisine": recipe.cuisine or "",
        "prep_time": recipe.prep_time,
        "cook_time": recipe.cook_time,
        "total_time": recipe.total_time,
        "servings": recipe.servings,
        "servings_text": recipe.servings_text or "",
        "rating": recipe.rating,
        "is_favourite": recipe.is_favourite,
        "notes": recipe.notes or "",
        "tags": list(recipe.tags),
        "courses": list(recipe.courses),
        "categories": list(recipe.categories),
        "collections": list(recipe.collections),
        "photos": list(recipe.photos),
        "ingredients": [_ingredient_to_dict(i) for i in recipe.ingredients],
        "instructions": list(recipe.instructions),
        "created_at": recipe.created_at,
        "updated_at": recipe.updated_at,
    }
    return attrs


class RecipeSensor(SensorEntity):
    """Read-only sensor mirroring one recipe.

    State = recipe name; everything else lives in the attributes so
    Jinja templates and auto-entities filters can work with it.
    """

    _attr_icon = "mdi:book-open-page-variant"
    _attr_should_poll = False

    def __init__(self, recipe: Recipe) -> None:
        self._recipe_id = recipe.id
        self._attr_unique_id = f"{DOMAIN}_recipe_{recipe.id}"
        self._apply(recipe)

    def _apply(self, recipe: Recipe) -> None:
        self._attr_name = (recipe.name or recipe.id)[:MAX_STATE_LEN]
        self._attr_native_value = (recipe.name or recipe.id)[:MAX_STATE_LEN]
        self._attr_extra_state_attributes = _recipe_attributes(recipe)

    def update_from_recipe(self, recipe: Recipe) -> None:
        self._apply(recipe)
        if self.hass and self.entity_id:
            self.async_write_ha_state()

    @property
    def recipe_id(self) -> str:
        return self._recipe_id


class RecipeSensorManager:
    """Creates / updates / removes RecipeSensor entities on bus events."""

    def __init__(
        self,
        hass: HomeAssistant,
        storage: RecipeStorage,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        self.hass = hass
        self.storage = storage
        self.async_add_entities = async_add_entities
        self._entities: Dict[str, RecipeSensor] = {}
        self._unsubs = []

    @callback
    def async_setup(self) -> None:
        # Initial population
        for recipe in self.storage.get_all_recipes():
            self._add_or_update(recipe)

        self._unsubs = [
            self.hass.bus.async_listen(EVENT_RECIPE_ADDED, self._on_added_or_updated),
            self.hass.bus.async_listen(EVENT_RECIPE_UPDATED, self._on_added_or_updated),
            self.hass.bus.async_listen(EVENT_RECIPE_DELETED, self._on_deleted),
        ]

    @callback
    def async_teardown(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    @callback
    def _on_added_or_updated(self, event: Event) -> None:
        recipe_id = event.data.get("recipe_id")
        if not recipe_id:
            return
        recipe = self.storage.get_recipe(recipe_id)
        if recipe is None:
            return
        self._add_or_update(recipe)

    @callback
    def _on_deleted(self, event: Event) -> None:
        recipe_id = event.data.get("recipe_id")
        entity = self._entities.pop(recipe_id, None)
        if entity is not None and entity.entity_id:
            _LOGGER.debug("Removing recipe entity for %s", recipe_id)
            # Removal: ask HA to drop the entity via the platform callback
            self._remove_entity(entity)

    def _add_or_update(self, recipe: Recipe) -> None:
        existing = self._entities.get(recipe.id)
        if existing is not None:
            existing.update_from_recipe(recipe)
            _LOGGER.debug("Updated recipe entity %s", recipe.name)
            return
        entity = RecipeSensor(recipe)
        self._entities[recipe.id] = entity
        self.async_add_entities([entity])
        _LOGGER.debug("Added recipe entity %s", recipe.name)

    def _remove_entity(self, entity: RecipeSensor) -> None:
        # async_add_entities gives us no direct remove handle; the registry
        # entry is cleaned when the platform reloads. For live removal we
        # erase its state and unregister from the entity registry.
        try:
            from homeassistant.helpers import entity_registry as er

            registry = er.async_get(self.hass)
            ent_id = registry.async_get_entity_id("sensor", DOMAIN, entity.unique_id)
            if ent_id:
                registry.async_remove(ent_id)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Could not remove recipe entity from registry: %s", exc)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    storage: Optional[RecipeStorage] = hass.data.get(DOMAIN, {}).get(DATA_STORAGE)
    if storage is None:
        _LOGGER.error("Recipe storage not available; sensor platform not set up")
        return

    manager = RecipeSensorManager(hass, storage, async_add_entities)
    manager.async_setup()
    hass.data[DOMAIN][DATA_SENSOR_MANAGER] = manager
