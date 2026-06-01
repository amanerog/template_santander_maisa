 <h3>
    Hey there, don't forget to read me!
    <img src="https://media.giphy.com/media/hvRJCLFzcasrR4ia7z/giphy.gif" width="30px"/>
 </h3>
<h1>GLN Component Template</h1>
The Component Template is designed to streamline the deployment of components to the Component Manager, integrating several CI/CD workflows into the process. Utilize this repository to efficiently create and publish a Component within the Gluon Portal.

### Comprehensive Guide to Project Structure and File Roles

| File | Description |
| --- | --- |
| setup.sh | This script orchestrates the invocation of the archetype and the management of the files. |
| presentation-schema.json | Configuration file that defines the structure and layout of the form, specifying what fields should be included and how they should be presented to the user. |
| definition.yml | Gluon Portal template metadata related to the component name, component version, the component description, the documentation link, contact information, etc.  |
| data-schema.json | Provides a description of a component's data structure. It contains information about the structure, properties, and other relevant details of the data associated with the component. |
| user-workflows-source.yml | Here you can find the list with the user workflows source repositories and versions to be included and validated in the component template. |

| Folder | Description |
| --- | --- |
| src | Contains the component's files to be used. |
| .github/workflows | Contains workflows to run automatically in the "Actions" tab. |

---
### Usage and instructions

- How to publish this Component Template?
To publish this Base Component Template, simply push to the main branch with changes and a different version in definition.yml file. This action will trigger the component-template-publish.yml workflow. 

### :hotsprings: Gluon Official Documentation
It's highly recommended to take a moment to read the entire [online documentation](https://gluon.gs.corp/community/docs/latest/contribute/base-component-template/) before starting to use this repository. By doing so, you can stay updated with workflow rules, new features, and other relevant information.
