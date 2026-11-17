"""Reference points specified before implementation from M-506:2005 Tables 1–2.

At fCO2=1 bar and S=19 Pa, fugacity and shear terms are both 1. These
constants independently exercise the tabulated Kt and pH branches.
"""
import pytest


@pytest.mark.parametrize('temperature,ph,expected', [
    (5,4,.48048), (15,4,1.81896), (20,4,5.447728),
    (40,4,10.212488), (60,4,11.858616), (50,4,11.035552),
    (60,6,2.034189), (150,6,.192511), (80,4,12.1208667),
    (90,4,7.914375), (90,6,.49125), (120,4,8.061375),
    (120,4.5,5.976684), (120,6,.92288175), (150,3.6,5.203),
    (150,4,3.621288), (80,5,6.005301679337918), (90,5,2.438496975074279),
])
def test_reference_points(temperature,ph,expected):
    from app.core.norsok import load_model, point_rate
    result=point_rate(temperature,1,19,ph,load_model())
    assert result == pytest.approx(expected,rel=1e-10)


def test_rate_responds_to_ph_and_wall_shear():
    from app.core.norsok import load_model, point_rate
    model=load_model()
    assert point_rate(60,1,19,4,model)>point_rate(60,1,19,6,model)
    assert point_rate(60,1,50,4,model)>point_rate(60,1,5,4,model)
    assert point_rate(5,1,50,4,model)==point_rate(5,1,5,4,model)


@pytest.mark.parametrize('values', [(4,1,19,4),(151,1,19,4),(60,.01,19,4),
    (60,11,19,4),(60,1,0,4),(60,1,151,4),(60,1,19,3),(60,1,19,7),
    (float('nan'),1,19,4),(60,float('inf'),19,4)])
def test_outside_model_domain_is_rejected(values):
    from app.core.norsok import load_model, point_rate
    with pytest.raises(ValueError):point_rate(*values,load_model())


def test_changing_configuration_changes_versioned_calculation(tmp_path):
    import json
    from pathlib import Path
    from app.core.norsok import load_model, point_rate
    model=load_model()
    data=json.loads(Path('config/co2_model.json').read_text())
    data['revision']='project-calibration-1';data['rate_multiplier']=1.2
    path=tmp_path/'calibrated.json';path.write_text(json.dumps(data))
    calibrated=load_model(path)
    assert calibrated['configuration_hash']!=model['configuration_hash']
    assert point_rate(60,1,19,4,calibrated)==pytest.approx(11.858616*1.2)


def state(**values):
    from app.schemas import CO2Calculation
    data=dict(well_id='MVP-1',temperature_c=60,pressure_abs_bar=100,co2_mole_fractions=[.01],
              composition_source='measured',ph_in_situ=4,wall_shear_pa=19,
              material='carbon_steel',water_wetting='water_wet')
    return CO2Calculation(**(data|values))


def test_active_model_calculates_and_records_configuration():
    from app.core.co2_corrosion import calculate
    expected_phi=10**(100*(.0031-1.4/333.15))
    result=calculate(state())
    assert result['model_version'].startswith('norsok-m506-2005')
    assert result['scenarios'][0]['fugacity_bar']==pytest.approx(expected_phi)
    assert result['scenarios'][0]['rate_mm_year']==pytest.approx(11.858616*expected_phi**.62)
    assert result['configuration_hash'] and result['model_snapshot']['knots']


@pytest.mark.parametrize('missing', ['ph_in_situ','wall_shear_pa'])
def test_missing_norsok_inputs_never_silently_use_base_model(missing):
    from app.core.co2_corrosion import calculate
    with pytest.raises(ValueError,match=missing):calculate(state(**{missing:None}))


@pytest.mark.parametrize('values', [{'water_wetting':'unknown'},{'water_wetting':'not_water_wet'},
                                  {'material':'other'},{'ph_in_situ':8}])
def test_applicability_is_explicit(values):
    from app.core.co2_corrosion import calculate
    with pytest.raises(ValueError):calculate(state(**values))


def test_water_cut_is_context_not_an_invented_rate_multiplier():
    from app.core.co2_corrosion import calculate
    a=calculate(state(water_cut_pct=30));b=calculate(state(water_cut_pct=80))
    assert a['scenarios'][0]['rate_mm_year']==b['scenarios'][0]['rate_mm_year']
    assert a['inputs']['water_cut_pct']==30 and b['inputs']['water_cut_pct']==80


def test_fugacity_pressure_cap_is_used_explicitly():
    from app.core.norsok import load_model,fugacity_coefficient
    model=load_model()
    assert fugacity_coefficient(60,300,model)==fugacity_coefficient(60,250,model)


@pytest.mark.parametrize('values', [dict(h2s_partial_bar=.06), dict(h2s_partial_bar=.049, co2_mole_fractions=[.005]),
                                  dict(organic_acids_ppm=101, co2_mole_fractions=[.004]), dict(model_id='missing')])
def test_other_chemistry_or_unknown_model_cannot_produce_misleading_results(values):
    from app.core.co2_corrosion import calculate
    with pytest.raises(ValueError): calculate(state(**values))


def test_active_alias_has_the_same_identity_as_configured_model():
    from app.core.co2_corrosion import calculate
    assert calculate(state())['input_hash'] == calculate(state(model_id='norsok-m506-2005'))['input_hash']


def test_composition_must_be_supplied_explicitly():
    from app.schemas import CO2Calculation
    with pytest.raises(ValueError): CO2Calculation(well_id='X', temperature_c=60, pressure_abs_bar=100)


@pytest.mark.parametrize('change', [{'algorithm': 'eval'}, {'rate_multiplier': -1}, {'revision': 'x'*64}])
def test_invalid_configuration_is_rejected(tmp_path, change):
    import json
    from pathlib import Path
    from app.core.norsok import load_model, ModelConfigurationError
    data = json.loads(Path('config/co2_model.json').read_text()) | change
    path = tmp_path/'model.json'; path.write_text(json.dumps(data))
    with pytest.raises(ModelConfigurationError): load_model(path)
