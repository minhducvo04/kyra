import pytest

from companion.cad.sheet import K_ESTIMATES, SheetSpec, bend_allowance


def test_bend_allowance_hand_calc_90_degrees():
    # 1.5708 * (2 + 0.44 * 2) = 1.5708 * 2.88 = 4.5239
    assert bend_allowance(90, 2, 0.44, 2) == pytest.approx(4.5239, abs=1e-3)


def test_spec_uses_its_own_k_for_the_allowance():
    spec = SheetSpec.for_material("Steel", 2, 2)
    assert spec.k_factor == K_ESTIMATES["steel"] and spec.material == "steel"
    assert spec.bend_allowance(90) == pytest.approx(4.5239, abs=1e-3)


def test_json_round_trip_is_equal():
    spec = SheetSpec.for_material("aluminium", 1.5, 1)
    assert SheetSpec.from_json(spec.to_json()) == spec


def test_unknown_material_raises_with_the_known_list():
    with pytest.raises(ValueError, match="unknown sheet material 'brass'.*aluminium.*stainless.*steel"):
        SheetSpec.for_material("brass", 2, 2)


@pytest.mark.parametrize("t,r,k", [(0, 2, 0.44), (-1, 2, 0.44), (2, -0.1, 0.44), (2, 2, 0), (2, 2, 1), (2, 2, 1.2),
                                   (float("nan"), 2, 0.44), (True, 2, 0.44)])
def test_bad_spec_numbers_are_refused(t, r, k):
    with pytest.raises(ValueError):
        SheetSpec(t, r, "steel", k)
    with pytest.raises(ValueError):
        bend_allowance(90, r, k, t)


@pytest.mark.parametrize("angle", [0, -5, 180.01, 360, float("nan"), "90"])
def test_bad_bend_angle_is_refused(angle):
    with pytest.raises(ValueError, match="angle"):
        bend_allowance(angle, 2, 0.44, 2)


def test_angle_180_is_allowed():
    assert bend_allowance(180, 2, 0.44, 2) == pytest.approx(3.14159265 * 2.88, abs=1e-3)


def test_from_json_refuses_a_bad_spec():
    bad = '{"thickness_mm": 0, "inside_radius_mm": 2, "material": "steel", "k_factor": 0.44, "k_is_estimate": true}'
    with pytest.raises(ValueError, match="thickness"):
        SheetSpec.from_json(bad)


def test_k_is_estimate_true_for_table_and_survives_round_trip():
    table = SheetSpec.for_material("stainless", 2, 2)
    assert table.k_is_estimate is True and SheetSpec.from_json(table.to_json()).k_is_estimate is True
    measured = SheetSpec(2, 2, "steel", 0.41, k_is_estimate=False)
    again = SheetSpec.from_json(measured.to_json())
    assert again == measured and again.k_is_estimate is False
