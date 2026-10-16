# Flowbench native Tree question

Flowbench's native Tree question is a single-select hierarchical application input. Configure one leaf path per line in the form `Parent > Child > Leaf`. Labels must be non-empty, paths must be canonical and unique, parent paths cannot also be selectable leaves, and nesting is limited to 16 levels. The selected value is the normalized full path string and can be bound through an existing Flowbench Action configuration path.

At run time the application form renders expandable branches and keyboard-operable leaf buttons. The engine validates a supplied or default answer against the declared leaf paths before applying Actions. The original workflow document remains unchanged because answers are applied to the run copy.

This is a Flowbench-native hierarchical choice control. The available public/reference records mention Alteryx Tree configurations for geography, variables and custom sources; those variants are not mapped here, and this work does not claim Alteryx XML interoperability or reference parity.
