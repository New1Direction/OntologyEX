# stripe-support-agent ontology

Four-layer ontology. Edit the YAML files, then:

    python scaffold.py validate .
    python scaffold.py mappings .

Layer rules: every L3 `binds` an L1 class; every L1 class has an `upper` anchor;
every L2 task's inputs/outputs are L1 classes.
