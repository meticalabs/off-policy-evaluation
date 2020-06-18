import argparse
from pathlib import Path
import yaml

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.experimental import enable_hist_gradient_boosting
from sklearn.ensemble import HistGradientBoostingClassifier

from dataset import OBDWithContextSets
from obp.simulator import OfflineBanditSimulator
from obp.policy import Random, BernoulliTS
from obp.utils import estimate_confidence_interval_by_bootstrap


cf_policy_dict = dict(bts=Random, random=BernoulliTS)  # Dict[behavior policy, counterfactual policy]

with open('./conf/lightgbm.yaml', 'rb') as f:
    hyperparams = yaml.safe_load(f)['model']

with open('./conf/prior_bts.yaml', 'rb') as f:
    production_prior_for_bts = yaml.safe_load(f)

with open('./conf/batch_size_bts.yaml', 'rb') as f:
    production_batch_size_for_bts = yaml.safe_load(f)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='evaluate off-policy estimators')
    parser.add_argument('--n_splits', '-n_s', type=int, default=1)
    parser.add_argument('--n_estimators', '-n_e', type=int, default=2)
    parser.add_argument('--behavior_policy', '-b_pol', type=str, choices=['bts', 'random'], required=True)
    parser.add_argument('--campaign', '-camp', type=str, choices=['all', 'men', 'women'], required=True)
    args = parser.parse_args()
    print(args)

    n_splits = args.n_splits
    n_estimators = args.n_estimators
    behavior_policy = args.behavior_policy
    campaign = args.campaign

    obd = OBDWithContextSets(
        behavior_policy=behavior_policy,
        campaign=campaign,
        data_path=Path('.').resolve().parents[1] / 'obd')

    random_state = 12345
    kwargs = dict(n_actions=obd.n_actions, len_list=obd.len_list, random_state=random_state)
    if behavior_policy == 'random':
        kwargs['alpha'] = production_prior_for_bts[campaign]['alpha']
        kwargs['beta'] = production_prior_for_bts[campaign]['beta']
        kwargs['batch_size'] = production_batch_size_for_bts[campaign]
    policy = cf_policy_dict[behavior_policy](**kwargs)

    np.random.seed(random_state)
    estimators = ['dm', 'ipw', 'dr']
    ope_results = {est: np.zeros(n_splits) for est in estimators}
    for random_state in np.arange(n_splits):
        train, test = obd.split_data(random_state=random_state)
        reward_test = test['reward']

        # define a regression model
        lightgbm = HistGradientBoostingClassifier(**hyperparams)
        regression_model = CalibratedClassifierCV(lightgbm, method='isotonic', cv=2)
        # bagging prediction for the policy value of the counterfactual policy
        ope_results_temp = {est: np.zeros(n_estimators) for est in estimators}
        for seed in np.arange(n_estimators):
            # run a bandit algorithm on logged bandit feedback
            # and conduct off-policy evaluation by using the result of the simulation
            train_boot = obd.sample_bootstrap(train=train)
            # regression_model = regression_model_dict[random_state]
            sim = OfflineBanditSimulator(
                train=train_boot, regression_model=regression_model, X_action=obd.X_action)
            sim.simulate(policy=policy)
            ope_results_temp['dm'][seed] = sim.direct_method()
            ope_results_temp['ipw'][seed] = sim.inverse_probability_weighting()
            ope_results_temp['dr'][seed] = sim.doubly_robust()

            policy.initialize()

        # results of OPE at each train-test split
        print('=' * 25)
        print(f'random_state={random_state}')
        print('-----')
        ground_truth = np.mean(reward_test)
        for est_name in estimators:
            estimated_policy_value = np.mean(ope_results_temp[est_name])
            relative_estimation_error_of_est = np.abs((estimated_policy_value - ground_truth) / ground_truth)
            ope_results[est_name][random_state] = relative_estimation_error_of_est
            print(f'{est_name.upper()}: {np.round(relative_estimation_error_of_est, 6)}')
        print('=' * 25, '\n')

    # estimate confidence intervals by nonparametric bootstrap method
    ope_results_with_ci = {est: dict() for est in estimators}
    for est_name in estimators:
        ope_results_with_ci[est_name] = estimate_confidence_interval_by_bootstrap(
            samples=ope_results[est_name], random_state=random_state)

    # save results of off-policy estimator selection
    log_path = Path('./logs') / behavior_policy / campaign
    pd.DataFrame(ope_results_with_ci).T.to_csv(log_path / 'off_policy_estimator_selection.csv')
