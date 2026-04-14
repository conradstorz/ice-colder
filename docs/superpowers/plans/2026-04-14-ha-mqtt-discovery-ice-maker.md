# HA MQTT Auto-Discovery for Ice Maker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the ice maker simulator's 9 temperature sensors, compressor state, and uptime automatically appear in Home Assistant via MQTT auto-discovery.

**Architecture:** The base class (`ESP32Simulator`) provides a discovery hook and publishing mechanism. Subclasses override `ha_discovery_entities()` to return entity definitions. On every MQTT connect, the base class publishes retained discovery config messages to `homeassistant/{component}/...` topics. The ice maker is the first subclass to implement this.

**Tech Stack:** Python 3.12, aiomqtt, Pydantic, pytest, asyncio

**Spec:** `docs/superpowers/specs/2026-04-14-ha-mqtt-discovery-ice-maker-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `simulators/base.py` | Modify | Add `ha_discovery_entities()` hook, `_build_ha_device()`, `_publish_ha_discovery()` |
| `simulators/ice_maker.py` | Modify | Override `ha_discovery_entities()` with 11 entity definitions |
| `tests/test_simulator_base.py` | Modify | Add tests for discovery publishing mechanics |
| `tests/test_simulator_ice_maker.py` | Modify | Add tests for ice maker entity definitions |

---

### Task 1: Base class discovery hook — tests

**Files:**
- Modify: `tests/test_simulator_base.py`

- [ ] **Step 1: Write tests for default discovery behavior and publishing mechanics**

Add the following test class at the end of `tests/test_simulator_base.py`:

```python
class TestHADiscovery:
    def test_default_returns_empty_list(self):
        sim = ConcreteSimulator()
        assert sim.ha_discovery_entities() == []

    def test_build_ha_device_block(self):
        sim = ConcreteSimulator(machine_id="vmc-0001")
        device = sim._build_ha_device()
        assert device["identifiers"] == ["vmc-0001_test_subsystem"]
        assert "name" in device
        assert device["manufacturer"] == "ice-colder"
        assert device["via_device"] == "vmc-0001"

    @pytest.mark.asyncio
    async def test_publish_ha_discovery_sends_retained_messages(self):
        sim = ConcreteSimulator(machine_id="vmc-0001")
        sim.ha_discovery_entities = lambda: [
            {
                "component": "sensor",
                "object_id": "fake_temp",
                "name": "Fake Temperature",
                "state_topic_suffix": "sensors/temp/fake",
                "value_template": "{{ value_json.value }}",
                "device_class": "temperature",
                "unit_of_measurement": "°C",
                "state_class": "measurement",
            },
        ]
        client = AsyncMock()
        await sim._publish_ha_discovery(client)

        client.publish.assert_called_once()
        call_args = client.publish.call_args
        topic = call_args[0][0] if call_args[0] else call_args.kwargs.get("topic", call_args[0][0])
        assert topic == "homeassistant/sensor/vmc-0001_test_subsystem/fake_temp/config"
        payload_str = call_args[0][1] if len(call_args[0]) > 1 else call_args.kwargs.get("payload")
        payload = json.loads(payload_str)
        assert payload["name"] == "Fake Temperature"
        assert payload["unique_id"] == "vmc-0001_test_subsystem_fake_temp"
        assert payload["state_topic"] == "vmc/vmc-0001/sensors/temp/fake"
        assert payload["device"]["identifiers"] == ["vmc-0001_test_subsystem"]
        assert call_args.kwargs.get("retain") is True

    @pytest.mark.asyncio
    async def test_publish_ha_discovery_empty_entities_no_publish(self):
        sim = ConcreteSimulator()
        client = AsyncMock()
        await sim._publish_ha_discovery(client)
        client.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_publish_ha_discovery_binary_sensor(self):
        sim = ConcreteSimulator(machine_id="vmc-0001")
        sim.ha_discovery_entities = lambda: [
            {
                "component": "binary_sensor",
                "object_id": "fake_running",
                "name": "Fake Running",
                "state_topic_suffix": "events/fake",
                "value_template": "{{ 'ON' if value_json.event == 'on' else 'OFF' }}",
                "device_class": "running",
                "payload_on": "ON",
                "payload_off": "OFF",
            },
        ]
        client = AsyncMock()
        await sim._publish_ha_discovery(client)

        call_args = client.publish.call_args
        topic = call_args[0][0] if call_args[0] else call_args.kwargs.get("topic", call_args[0][0])
        assert "binary_sensor" in topic
        payload = json.loads(call_args[0][1] if len(call_args[0]) > 1 else call_args.kwargs.get("payload"))
        assert payload["payload_on"] == "ON"
        assert payload["payload_off"] == "OFF"
```

You also need to add `import json` at the top of the file (it is not currently imported).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_simulator_base.py::TestHADiscovery -v --ignore=colder-docker`

Expected: FAIL — `AttributeError: 'ConcreteSimulator' object has no attribute 'ha_discovery_entities'`

- [ ] **Step 3: Commit**

```bash
git add tests/test_simulator_base.py
git commit -m "test: add failing tests for HA MQTT discovery in base simulator"
```

---

### Task 2: Base class discovery hook — implementation

**Files:**
- Modify: `simulators/base.py:33-46` (constructor), add new methods after `_topic_matches` (line 116)

- [ ] **Step 1: Add `ha_discovery_entities()` hook and `_build_ha_device()` to ESP32Simulator**

Add these three methods to the `ESP32Simulator` class, after the `_topic_matches` static method (after line 116) and before the `run_simulation` abstract method:

```python
    def ha_discovery_entities(self) -> list[dict]:
        """Override in subclasses to return HA discovery entity definitions.

        Each dict should have keys: component, object_id, name, state_topic_suffix,
        value_template. Optional: device_class, unit_of_measurement, state_class,
        payload_on, payload_off, expire_after.
        """
        return []

    def _build_ha_device(self) -> dict:
        """Build the HA device block shared by all entities from this simulator."""
        subsystem_display = self.subsystem_name.replace("_", " ").title()
        machine_name = self.config.physical.common_name
        return {
            "identifiers": [f"{self.machine_id}_{self.subsystem_name}"],
            "name": f"{machine_name} {subsystem_display}",
            "manufacturer": "ice-colder",
            "model": f"ESP32 {self.subsystem_name} simulator",
            "via_device": self.machine_id,
        }

    async def _publish_ha_discovery(self, client: aiomqtt.Client):
        """Publish HA MQTT auto-discovery config for all entities."""
        entities = self.ha_discovery_entities()
        if not entities:
            return
        device = self._build_ha_device()
        node_id = f"{self.machine_id}_{self.subsystem_name}"
        for entity in entities:
            component = entity["component"]
            object_id = entity["object_id"]
            topic = f"homeassistant/{component}/{node_id}/{object_id}/config"
            payload = {
                "name": entity["name"],
                "unique_id": f"{node_id}_{object_id}",
                "state_topic": f"{self.topic_prefix}/{entity['state_topic_suffix']}",
                "value_template": entity["value_template"],
                "device": device,
            }
            for optional_key in (
                "device_class", "unit_of_measurement", "state_class",
                "payload_on", "payload_off", "expire_after",
            ):
                if optional_key in entity:
                    payload[optional_key] = entity[optional_key]
            await client.publish(topic, json.dumps(payload), retain=True)
            logger.debug(f"[{self.subsystem_name}] HA discovery: {topic}")
```

- [ ] **Step 2: Call `_publish_ha_discovery()` on connect in `run()`**

In the `run()` method (line 122), add a call to `_publish_ha_discovery` right after the log message and before `self._subscriptions.clear()`. The modified section of `run()` should look like:

```python
            async with aiomqtt.Client(
                hostname=self.broker,
                port=self.port,
                identifier=f"sim-{self.subsystem_name}",
            ) as client:
                logger.info(
                    f"[{self.subsystem_name}] Connected to {self.broker}:{self.port}"
                )
                self._subscriptions.clear()
                await self._publish_ha_discovery(client)
                async with asyncio.TaskGroup() as tg:
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `uv run pytest tests/test_simulator_base.py -v --ignore=colder-docker`

Expected: All tests PASS, including the 5 new `TestHADiscovery` tests.

- [ ] **Step 4: Run full test suite to check for regressions**

Run: `uv run pytest --ignore=colder-docker -q`

Expected: All 167+ tests pass.

- [ ] **Step 5: Commit**

```bash
git add simulators/base.py
git commit -m "feat: add HA MQTT auto-discovery hook to ESP32Simulator base class"
```

---

### Task 3: Ice maker discovery entities — tests

**Files:**
- Modify: `tests/test_simulator_ice_maker.py`

- [ ] **Step 1: Write tests for ice maker HA discovery entities**

Add the following test class at the end of `tests/test_simulator_ice_maker.py`:

```python
class TestHADiscovery:
    def test_returns_11_entities(self):
        sim = IceMakerSimulator()
        entities = sim.ha_discovery_entities()
        assert len(entities) == 11

    def test_nine_temperature_sensors(self):
        sim = IceMakerSimulator()
        entities = sim.ha_discovery_entities()
        temp_sensors = [e for e in entities if e.get("device_class") == "temperature"]
        assert len(temp_sensors) == 9

    def test_temperature_sensor_fields(self):
        sim = IceMakerSimulator()
        entities = sim.ha_discovery_entities()
        temp = next(e for e in entities if e["object_id"] == "water_inlet_temp")
        assert temp["component"] == "sensor"
        assert temp["name"] == "Ice Maker Water Inlet Temperature"
        assert temp["state_topic_suffix"] == "sensors/temp/water_inlet"
        assert temp["value_template"] == "{{ value_json.value }}"
        assert temp["unit_of_measurement"] == "\u00b0C"
        assert temp["state_class"] == "measurement"
        assert temp["expire_after"] == 30

    def test_compressor_binary_sensor(self):
        sim = IceMakerSimulator()
        entities = sim.ha_discovery_entities()
        comp = next(e for e in entities if e["object_id"] == "compressor")
        assert comp["component"] == "binary_sensor"
        assert comp["name"] == "Ice Maker Compressor"
        assert comp["device_class"] == "running"
        assert comp["state_topic_suffix"] == "ice_maker/event"
        assert comp["payload_on"] == "ON"
        assert comp["payload_off"] == "OFF"

    def test_uptime_sensor(self):
        sim = IceMakerSimulator()
        entities = sim.ha_discovery_entities()
        uptime = next(e for e in entities if e["object_id"] == "uptime")
        assert uptime["component"] == "sensor"
        assert uptime["name"] == "Ice Maker Uptime"
        assert uptime["device_class"] == "duration"
        assert uptime["unit_of_measurement"] == "s"
        assert uptime["state_class"] == "total_increasing"
        assert uptime["state_topic_suffix"] == "heartbeat/ice_maker"

    def test_all_state_topic_suffixes_are_valid(self):
        """Verify every entity points to a topic the simulator actually publishes to."""
        sim = IceMakerSimulator()
        entities = sim.ha_discovery_entities()
        valid_prefixes = {"sensors/temp/", "ice_maker/event", "heartbeat/ice_maker"}
        for entity in entities:
            suffix = entity["state_topic_suffix"]
            assert any(suffix.startswith(p) or suffix == p for p in valid_prefixes), \
                f"Unexpected state_topic_suffix: {suffix}"

    def test_all_object_ids_unique(self):
        sim = IceMakerSimulator()
        entities = sim.ha_discovery_entities()
        ids = [e["object_id"] for e in entities]
        assert len(ids) == len(set(ids))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_simulator_ice_maker.py::TestHADiscovery -v --ignore=colder-docker`

Expected: FAIL — `AttributeError` or empty list returned (base class default).

- [ ] **Step 3: Commit**

```bash
git add tests/test_simulator_ice_maker.py
git commit -m "test: add failing tests for ice maker HA discovery entities"
```

---

### Task 4: Ice maker discovery entities — implementation

**Files:**
- Modify: `simulators/ice_maker.py:59-76` (IceMakerSimulator class)

- [ ] **Step 1: Override `ha_discovery_entities()` in IceMakerSimulator**

Add this method to the `IceMakerSimulator` class, after the `__init__` method (after line 76) and before the `tick` method:

```python
    def ha_discovery_entities(self) -> list[dict]:
        """Return HA discovery definitions for ice maker sensors."""
        entities = []
        # 9 temperature sensors — one per thermal sensor
        for sensor in self.sensors:
            entities.append({
                "component": "sensor",
                "object_id": f"{sensor.name}_temp",
                "name": f"Ice Maker {sensor.name.replace('_', ' ').title()} Temperature",
                "state_topic_suffix": f"sensors/temp/{sensor.name}",
                "value_template": "{{ value_json.value }}",
                "device_class": "temperature",
                "unit_of_measurement": "\u00b0C",
                "state_class": "measurement",
                "expire_after": 30,
            })
        # Compressor binary sensor
        entities.append({
            "component": "binary_sensor",
            "object_id": "compressor",
            "name": "Ice Maker Compressor",
            "state_topic_suffix": "ice_maker/event",
            "value_template": "{{ 'ON' if value_json.event == 'power_on' else 'OFF' }}",
            "device_class": "running",
            "payload_on": "ON",
            "payload_off": "OFF",
        })
        # Uptime sensor from heartbeat
        entities.append({
            "component": "sensor",
            "object_id": "uptime",
            "name": "Ice Maker Uptime",
            "state_topic_suffix": "heartbeat/ice_maker",
            "value_template": "{{ value_json.uptime_seconds }}",
            "device_class": "duration",
            "unit_of_measurement": "s",
            "state_class": "total_increasing",
        })
        return entities
```

- [ ] **Step 2: Run ice maker tests to verify they pass**

Run: `uv run pytest tests/test_simulator_ice_maker.py -v --ignore=colder-docker`

Expected: All tests PASS, including the 7 new `TestHADiscovery` tests.

- [ ] **Step 3: Run full test suite to check for regressions**

Run: `uv run pytest --ignore=colder-docker -q`

Expected: All tests pass (174+ tests).

- [ ] **Step 4: Commit**

```bash
git add simulators/ice_maker.py
git commit -m "feat: add HA MQTT auto-discovery entities to ice maker simulator"
```

---

### Task 5: Integration smoke test

**Files:**
- Modify: `tests/test_simulator_ice_maker.py`

- [ ] **Step 1: Write integration test for full discovery publish flow**

Add this test to the `TestHADiscovery` class in `tests/test_simulator_ice_maker.py`:

```python
    @pytest.mark.asyncio
    async def test_discovery_publishes_all_entities(self):
        """Smoke test: the base class publishes all 11 ice maker entities."""
        from unittest.mock import AsyncMock
        sim = IceMakerSimulator(machine_id="vmc-test")
        client = AsyncMock()
        await sim._publish_ha_discovery(client)
        assert client.publish.call_count == 11
        topics = [call.args[0] for call in client.publish.call_args_list]
        # All should be under homeassistant/
        assert all(t.startswith("homeassistant/") for t in topics)
        # All should contain the machine_id
        assert all("vmc-test_ice_maker" in t for t in topics)
        # All should be retained
        assert all(call.kwargs.get("retain") is True for call in client.publish.call_args_list)
```

Also add `import pytest` at the top of the file if not already present (it is already there).

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_simulator_ice_maker.py::TestHADiscovery::test_discovery_publishes_all_entities -v --ignore=colder-docker`

Expected: PASS

- [ ] **Step 3: Run full test suite one final time**

Run: `uv run pytest --ignore=colder-docker -q`

Expected: All tests pass (175+ tests).

- [ ] **Step 4: Commit**

```bash
git add tests/test_simulator_ice_maker.py
git commit -m "test: add integration smoke test for ice maker HA discovery publishing"
```
