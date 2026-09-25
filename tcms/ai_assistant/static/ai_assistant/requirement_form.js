(function () {
    var form = document.getElementById("generation-form");
    var generateButton = document.getElementById("generate-button");

    function showProgress(event, action) {
        var submittedForm = event.currentTarget;
        if (submittedForm.dataset.submitting === "true") {
            event.preventDefault();
            return;
        }
        submittedForm.dataset.submitting = "true";
        // Disabled buttons are omitted from POST data. Preserve the chosen
        // action before disabling them so analysis cannot become generation.
        if (submittedForm === form) {
            var actionInput = document.createElement("input");
            actionInput.type = "hidden";
            actionInput.name = "action";
            actionInput.value = action;
            submittedForm.appendChild(actionInput);
        }
        submittedForm.querySelectorAll('button[type="submit"]').forEach(function (button) {
            button.disabled = true;
        });
        document.getElementById("generation-progress").style.display = "block";
    }

    window.addEventListener("pageshow", function (event) {
        if (event.persisted) { window.location.reload(); }
    });

    if (form) {
        form.addEventListener("submit", function (event) {
            var clickedButton = event.submitter || generateButton;
            showProgress(event, clickedButton.value || "generate");
        });
    }
    var analysisForms = document.querySelectorAll(".analysis-generation-form");
    for (var index = 0; index < analysisForms.length; index++) {
        analysisForms[index].addEventListener("submit", function (event) {
            showProgress(event, "generate");
        });
    }
    var coverageForms = document.querySelectorAll(".coverage-analysis-form");
    for (var coverageIndex = 0; coverageIndex < coverageForms.length; coverageIndex++) {
        coverageForms[coverageIndex].addEventListener("submit", function (event) {
            showProgress(event, "coverage");
        });
    }
    var supplementForms = document.querySelectorAll(".gap-supplement-form");
    for (var supplementIndex = 0; supplementIndex < supplementForms.length; supplementIndex++) {
        supplementForms[supplementIndex].addEventListener("submit", function (event) {
            showProgress(event, "supplement");
        });
    }
}());
